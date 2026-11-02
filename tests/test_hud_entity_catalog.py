from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aios_app.hud.entity_catalog import NOTICE, TITLE, hud_catalog, present_catalog
from aios_app.hud.frame import HUDAssembler
from aios_app.hud.render_text import render_hud_text


def catalog():
    kitchen = {"id": "place:1", "label": "Kitchen", "kind": "place"}
    mug = {"id": "entity:1", "label": "blue mug", "kind": "object"}
    bob = {"id": "person:1", "label": "Bob", "kind": "person"}
    return {
        "graph": "graph:1",
        "places": [
            {
                "place": kitchen,
                "contents": [
                    {
                        "id": "edge:1",
                        "entity": mug,
                        "relation": "in",
                        "status": "reported",
                        "source": "source:1",
                        "source_text": "The blue mug is in the kitchen.",
                    }
                ],
                "other_evidence": [],
            }
        ],
        "people": [bob],
        "unplaced": [bob],
        "familiarity": [
            {"entity_id": entity["id"], "mode": "source_mention", "source": "source:1"}
            for entity in [kitchen, mug, bob]
        ],
    }


def test_prompt_groups_reported_contents_without_present_or_inventory_claims():
    result = present_catalog(catalog(), token_cap=500)
    text = render_hud_text(
        {
            "entity_catalog": result,
            "scene": {
                "actors": [{"display_name": "Hidden neighbor"}],
                "objects": [{"display_name": "Hidden item"}],
            },
        }
    )
    assert TITLE in text and NOTICE in text
    assert "Kitchen" in text and "blue mug" in text and "reported; mentioned" in text
    assert "Bob" in text and "current location unverified" in text
    assert "PRESENT PEOPLE" not in text and "PRESENT OBJECTS" not in text
    assert "Hidden neighbor" not in text and "Hidden item" not in text
    assert result["entries"][0]["sources"] == ["source:1"]
    assert result["entries"][0]["evidence"]["id"] == "edge:1"


@pytest.mark.parametrize(
    "status,sentence",
    [
        ("denied", "There is no blue mug in the kitchen."),
        ("qualified", "If a blue mug were in the kitchen, I would use it."),
        ("illustrative", "For example, a blue mug in a kitchen."),
    ],
)
def test_nonpositive_evidence_keeps_qualification_and_original_excerpt(
    status, sentence
):
    data = catalog()
    edge = data["places"][0]["contents"].pop()
    edge.update(status=status, source_text=sentence)
    data["places"][0]["other_evidence"].append(edge)
    text = render_hud_text({"entity_catalog": present_catalog(data, token_cap=500)})
    assert status in text and sentence in text


def test_same_named_people_keep_distinct_identity_and_knowledge_modes():
    data = catalog()
    data["places"] = []
    data["people"] = [
        {"id": f"person:{i}", "label": "Bob", "kind": "person"} for i in range(1, 4)
    ]
    data["unplaced"] = []
    data["familiarity"] = [
        {"entity_id": f"person:{i}", "mode": mode, "source": f"source:{i}"}
        for i, mode in enumerate(
            ["source_mention", "encounter_record", "corpus_reference_exposure"], 1
        )
    ]
    result = present_catalog(data, token_cap=500)
    assert len({e["entity_id"] for e in result["entries"]}) == 3
    text = render_hud_text({"entity_catalog": result})
    assert (
        "mentioned" in text
        and "encounter recorded" in text
        and "corpus exposure" in text
    )


def test_focus_ranks_whole_rows_within_budget_and_keeps_opaque_references():
    data = catalog()
    result = present_catalog(data, token_cap=75, focus_text="Bob")
    assert len(result["entries"]) == 1
    assert result["entries"][0]["label"] == "Bob"
    rendered = (
        TITLE
        + "\n"
        + NOTICE
        + "\n"
        + "\n".join("- " + row["text"] for row in result["entries"])
    )
    assert len(rendered) <= 300
    assert result["truncated"] is True
    assert present_catalog(data, token_cap=0)["entries"] == []


@pytest.mark.asyncio
async def test_hud_lookup_reuses_exact_context_and_degrades_to_unknown(monkeypatch):
    import aios_app.hud.entity_catalog as module

    context = SimpleNamespace(instance_id=uuid4(), location_entity_id=None)
    read = AsyncMock(return_value=catalog())
    monkeypatch.setattr(module.EntityCatalogService, "character", read)
    result = await hud_catalog(object(), context, token_cap=260)
    assert result["status"] == "ready"
    read.assert_awaited_once_with(context.instance_id, context=context)
    assert await hud_catalog(object(), context, token_cap=0) is None
    assert read.await_count == 1
    read.side_effect = TimeoutError()
    result = await hud_catalog(object(), context, token_cap=260)
    text = render_hud_text({"entity_catalog": result})
    assert "Catalog unavailable" in text and "remain unknown" in text


@pytest.mark.asyncio
async def test_scene_does_not_expand_world_graph_neighbors():
    db = SimpleNamespace(fetchrow=AsyncMock(return_value={"display_name": "Kitchen"}))
    assembler = HUDAssembler.__new__(HUDAssembler)
    assembler.db = db
    context = SimpleNamespace(world_id=uuid4(), location_entity_id=uuid4())
    scene = await assembler._scene(context)
    assert scene == {
        "location": {"display_name": "Kitchen"},
        "actors": [],
        "objects": [],
        "relations": [],
    }
    assert db.fetchrow.call_args.args[1:] == (
        context.world_id,
        context.location_entity_id,
    )
    context.location_entity_id = None
    assert (await assembler._scene(context))["location"] is None
    assert db.fetchrow.await_count == 1
