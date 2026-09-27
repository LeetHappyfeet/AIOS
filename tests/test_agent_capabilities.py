from __future__ import annotations

from uuid import UUID

import pytest

from aios_app.agent.actions import default_action_registry
from aios_app.agent.capabilities import ResearchRouter


def test_default_registry_exposes_new_capability_layer():
    registry = default_action_registry(object())

    research = registry.capabilities_for("research")
    assert research["knowledge.lookup"]["class"] == "lookup"
    assert research["state.inspect"]["class"] == "lookup"
    assert research["research.search"]["class"] == "research"
    assert research["source.read"]["class"] == "research"
    assert research["research.search"]["result_mode"] == "return_to_cognition"
    assert research["corpus.acquire"]["result_mode"] == "final"

    communication = registry.capabilities_for("communication")
    assert "state.inspect" in communication
    assert "research.search" not in communication
    assert "source.read" not in communication


def test_inference_schema_contract_remains_plain_action_schemas():
    registry = default_action_registry(object())
    schemas = registry.schemas_for("research")

    assert schemas["research.search"]["type"] == "object"
    assert "class" not in schemas["research.search"]
    assert "side_effect_class" not in schemas["research.search"]


class _ReadDB:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.row


@pytest.mark.asyncio
async def test_source_read_requires_exposure_for_same_instance():
    instance_id = UUID("00000000-0000-0000-0000-000000000001")
    research_id = UUID("00000000-0000-0000-0000-000000000002")
    section_id = UUID("00000000-0000-0000-0000-000000000003")
    db = _ReadDB(None)

    with pytest.raises(PermissionError):
        await ResearchRouter(db).read_source(
            instance_id=instance_id,
            research_id=research_id,
            section_id=section_id,
        )

    assert db.calls
    _, args = db.calls[0]
    assert args == (research_id, section_id, instance_id)


@pytest.mark.asyncio
async def test_source_read_returns_reference_not_durable_knowledge():
    instance_id = UUID("00000000-0000-0000-0000-000000000001")
    research_id = UUID("00000000-0000-0000-0000-000000000002")
    section_id = UUID("00000000-0000-0000-0000-000000000003")
    document_id = UUID("00000000-0000-0000-0000-000000000004")
    db = _ReadDB({
        "section_id": section_id,
        "document_id": document_id,
        "title": "Reference",
        "heading": "Details",
        "content": "Full source text",
        "score": 0.8,
        "rank": 1,
        "scopes": ["fiction.test.reference"],
    })

    result = await ResearchRouter(db).read_source(
        instance_id=instance_id,
        research_id=research_id,
        section_id=section_id,
    )

    assert result["text"] == "Full source text"
    assert result["durable_knowledge"] is False
    assert result["research_id"] == str(research_id)
    assert result["section_id"] == str(section_id)
