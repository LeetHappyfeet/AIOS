"""Particular places, qualified contents, character scope, and read-only exports."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from aios_app.corpus_rdf.service import rdf_update as corpus_update
from aios_app.entity_memory.catalog import (
    NS,
    catalog_trig,
    reference_view,
    world_entity_iri,
)
from aios_app.entity_memory.catalog_api import catalog_router
from aios_app.entity_memory.catalog_service import EntityCatalogService
from aios_app.entity_memory.service import rdf_update
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Dataset, URIRef
from test_entity_memory_postgres import env as base_db  # noqa: F401 -- fixture dependency
from test_rdf_recall_postgres import env as recall_env  # noqa: F401 -- pytest fixture

ROOT = Path(__file__).resolve().parents[1]


def payload():
    room, bed, person = [str(uuid4()) for _ in range(3)]
    return {
        "section_id": str(uuid4()),
        "policy_version": "test",
        "scope": "test",
        "contexts": [],
        "truncated": False,
        "anchors": [
            {
                "entity_id": room,
                "label": "Mia’s bedroom",
                "kind": "place",
                "status": "candidate",
            },
            {
                "entity_id": bed,
                "label": "the bed",
                "kind": "unknown",
                "status": "candidate",
            },
            {
                "entity_id": person,
                "label": "Bob",
                "kind": "person",
                "status": "candidate",
            },
        ],
        "mentions": [],
        "relations": [
            {
                "subject": bed,
                "predicate": "located_in",
                "object": room,
                "scope": "asserted",
                "polarity": 1,
                "source_text": "The bed is in Mia’s bedroom.",
                "status": "evidence_only",
                "predicate_offset": 8,
                "modal": [],
                "attribution": [],
                "tense": [],
                "aspect": [],
            }
        ],
    }


def test_reference_places_preserve_instance_ids_and_qualified_membership():
    p = payload()
    view = reference_view(p)
    assert len(view["places"]) == 1
    assert view["places"][0]["contents"][0]["entity"]["label"] == "the bed"
    assert view["places"][0]["contents"][0]["status"] == "reported"
    graph = Dataset()
    graph.update(rdf_update(p["section_id"], p))
    q = f"""PREFIX c:<{NS}> SELECT ?member WHERE {{ GRAPH ?g {{
      ?place a c:Place; c:hasMembershipEvidence ?e.
      ?e c:member ?member; c:membershipStatus "reported".
    }} }}"""
    assert len(list(graph.query(q))) == 1
    assert not list(graph.quads((None, URIRef(NS + "locatedIn"), None, None)))
    assert view["people"][0]["label"] == "Bob"
    p2 = payload()
    assert (
        reference_view(p2)["places"][0]["place"]["id"]
        != view["places"][0]["place"]["id"]
    )


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"polarity": -1}, "denied"),
        ({"modal": ["might"]}, "qualified"),
        ({"scope": "conditional"}, "qualified"),
        ({"attribution": [{"predicate": "say"}]}, "qualified"),
        ({"content_role": "example"}, "illustrative"),
        ({"predicate": "located_near"}, "reported"),
    ],
)
def test_nonpositive_or_near_evidence_does_not_populate_contents(changes, status):
    p = payload()
    p["relations"][0].update(changes)
    place = reference_view(p)["places"][0]
    assert not place["contents"] and place["other_evidence"][0]["status"] == status


def test_char_instance_references_are_not_encounters_or_shared_character_knowledge():
    p = payload()
    instance = uuid4()
    p["contexts"] = [{"character_instance_id": str(instance)}]
    graph = Dataset()
    graph.update(rdf_update(p["section_id"], p))
    references = list(
        graph.quads(
            (
                URIRef("urn:aios:character-instance:" + str(instance)),
                URIRef(NS + "hasEntityReference"),
                None,
                None,
            )
        )
    )
    assert len(references) == 3
    assert all(
        str(mode) == "source_mention"
        for _, _, mode, _ in graph.quads(
            (None, URIRef(NS + "knowledgeMode"), None, None)
        )
    )
    other = uuid4()
    assert not list(
        graph.quads(
            (URIRef("urn:aios:character-instance:" + str(other)), None, None, None)
        )
    )


def test_corpus_examples_are_illustrative_and_keep_corpus_identifiers():
    p = payload()
    p.update(
        document_id=str(uuid4()),
        namespace="canon.test",
        reference_kind="dictionary",
        identity_binding="external",
        roles=[],
        scopes=[],
        domains=[],
    )
    p["relations"][0].update(content_role="example", authority="illustrative_only")
    view = reference_view(p, corpus=True)
    assert not view["places"][0]["contents"]
    assert view["places"][0]["other_evidence"][0]["status"] == "illustrative"
    assert view["people"][0]["id"].startswith("urn:aios:corpus:entity:")
    graph = Dataset()
    graph.update(corpus_update(p["section_id"], p))
    assert list(graph.quads((None, URIRef(NS + "hasMembershipEvidence"), None, None)))
    assert not list(graph.quads((None, URIRef(NS + "knowledgeMode"), None, None)))


@pytest_asyncio.fixture
async def env(recall_env, monkeypatch):  # noqa: F811 -- pytest fixture dependency
    e = recall_env
    await e.db.pool.con.query(
        """
      CREATE TABLE aios.world_entity(entity_id uuid PRIMARY KEY,world_id uuid,entity_type text,display_name text);
      CREATE TABLE aios.world_entity_relation(relation_id uuid PRIMARY KEY,world_id uuid,subject_entity_id uuid,
        object_entity_id uuid,relation_type text,meta jsonb DEFAULT '{}',valid_from_node_id uuid,
        valid_to_node_id uuid,created_at timestamptz DEFAULT now());
      CREATE TABLE aios.character_relationship(relationship_id uuid PRIMARY KEY,observer_instance_id uuid,
        target_entity_id uuid,relationship_type text,meta jsonb DEFAULT '{}',updated_at timestamptz DEFAULT now());
      CREATE TABLE aios.corpus_reference_projection(section_id uuid PRIMARY KEY,payload jsonb,
        revision bigint DEFAULT 1,built_revision bigint DEFAULT 1,rdf_revision bigint DEFAULT 1,updated_at timestamptz DEFAULT now());
      CREATE TABLE aios.corpus_section(section_id uuid PRIMARY KEY,document_id uuid,content text);
      CREATE TABLE aios.corpus_document(document_id uuid PRIMARY KEY);
      CREATE TABLE aios.corpus_document_scope(document_id uuid,scope_key text);
      CREATE TABLE aios.corpus_scope(scope_key text,access_class text);
      CREATE TABLE aios.corpus_document_domain(document_id uuid,knowledge_domain text);
      CREATE TABLE aios.character_knowledge_domain(character_id text,knowledge_domain text,enabled boolean);
      CREATE TABLE aios.corpus_document_collection(document_id uuid,profile_id uuid);
      CREATE TABLE aios.corpus_source_profile(profile_id uuid,enabled boolean,scope_key text,knowledge_domain text);
      CREATE TABLE aios.character_corpus_access(character_id text,scope_key text,allowed boolean);
      CREATE TABLE aios.character_corpus_exposure(research_id uuid,section_id uuid,exposed_at timestamptz DEFAULT now());
      CREATE TABLE aios.source_consumption(consumption_id uuid PRIMARY KEY,section_id uuid,instance_id uuid,status text,consumed_at timestamptz DEFAULT now());
      ALTER TABLE aios.knowledge_acquisition_event ADD COLUMN consumption_id uuid;
      CREATE TABLE aios.character_research_event(research_id uuid PRIMARY KEY,instance_id uuid);
    """,
        exec=True,
    )
    from aios_app.hud.context import HUDContextResolver

    async def resolve(self, instance_id):
        assert instance_id == e.ctx.instance_id
        return e.ctx

    monkeypatch.setattr(HUDContextResolver, "resolve", resolve)
    e.catalog = EntityCatalogService(e.db)
    return e


@pytest.mark.asyncio
async def test_character_catalog_uses_accessible_sources_without_writes_or_fuseki(env):
    e = env
    result = await e.catalog.character(e.ctx.instance_id)
    assert result["places"] and "kitchen" in result["places"][0]["place"]["label"]
    assert result["places"][0]["contents"][0]["entity"]["label"] == "a blue mug"
    assert {r["mode"] for r in result["familiarity"]} == {"source_mention"}
    assert await e.db.fetchval("SELECT count(*) FROM aios.entity_recall_trace") == 0
    assert e.graphs.reads == 0
    graph = Dataset()
    graph.parse(data=catalog_trig(result), format="trig")
    assert len(graph) > 0
    # A different branch cannot inherit source-local objects just by name.
    e.ctx.source_head_node_id = uuid4()
    assert not (await e.catalog.character(e.ctx.instance_id))["entities"]


@pytest.mark.asyncio
async def test_hud_catalog_uses_authorized_sql_view_and_withdraws_edited_source(env):
    from aios_app.hud.entity_catalog import hud_catalog
    from aios_app.hud.render_text import render_hud_text

    env.ctx.location_entity_id = None
    result = await hud_catalog(env.db, env.ctx, token_cap=400, focus_text="blue mug")
    assert result["status"] == "ready"
    text = render_hud_text({"entity_catalog": result})
    assert "blue mug" in text and "reported" in text
    assert env.graphs.reads == 0
    await env.db.execute(
        "UPDATE aios.document_section SET content='edited' WHERE section_id=$1",
        env.section,
    )
    after = await hud_catalog(env.db, env.ctx, token_cap=400)
    assert after["status"] == "ready" and not after["entries"]


@pytest.mark.asyncio
async def test_character_catalog_graph_tracks_cognitive_scope_revision(env):
    env.ctx.cognitive_revision = "before"
    before = await env.catalog.character(env.ctx.instance_id)
    env.ctx.cognitive_revision = "after"
    after = await env.catalog.character(env.ctx.instance_id)
    assert before["graph"] != after["graph"]
    assert before["entities"] == after["entities"]


@pytest.mark.asyncio
async def test_edited_or_superseded_sources_withdraw_from_character_catalog(env):
    e = env
    await e.db.execute(
        "UPDATE aios.document_section SET content='changed' WHERE section_id=$1",
        e.section,
    )
    assert not (await e.catalog.character(e.ctx.instance_id))["entities"]
    await e.db.execute(
        "UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=1"
    )
    assert not (await e.catalog.character(e.ctx.instance_id))["entities"]


@pytest.mark.asyncio
async def test_world_catalog_requires_matching_branch_and_keeps_places_distinct(env):
    e = env
    room, bed, otherroom, otherbed = [uuid4() for _ in range(4)]
    for entity, kind, name in [
        (room, "bedroom", "Bedroom A"),
        (bed, "object", "Bed A"),
        (otherroom, "bedroom", "Bedroom B"),
        (otherbed, "object", "Bed B"),
    ]:
        await e.db.execute(
            "INSERT INTO aios.world_entity VALUES($1,$2,$3,$4)",
            entity,
            e.ctx.world_id,
            kind,
            name,
        )
    await e.db.execute(
        "INSERT INTO aios.world_entity_relation(relation_id,world_id,subject_entity_id,object_entity_id,relation_type,valid_from_node_id) VALUES($1,$2,$3,$4,$5,$6)",
        uuid4(),
        e.ctx.world_id,
        bed,
        room,
        "located_in",
        e.ctx.source_head_node_id,
    )
    sibling = uuid4()
    await e.db.execute(
        "INSERT INTO aios.dag_node(node_id,timeline_id,event_id) VALUES($1,$2,1)",
        sibling,
        e.ctx.source_timeline_id,
    )
    await e.db.execute(
        "INSERT INTO aios.world_entity_relation(relation_id,world_id,subject_entity_id,object_entity_id,relation_type,valid_from_node_id) VALUES($1,$2,$3,$4,$5,$6)",
        uuid4(),
        e.ctx.world_id,
        otherbed,
        otherroom,
        "located_in",
        sibling,
    )
    result = await e.catalog.world(
        e.ctx.world_id, e.ctx.source_timeline_id, e.ctx.source_head_node_id
    )
    assert [p["place"]["label"] for p in result["places"]] == ["Bedroom A"]
    assert result["places"][0]["contents"][0]["entity"]["id"] == world_entity_iri(bed)
    assert (await e.catalog.character(e.ctx.instance_id))["familiarity"][0][
        "mode"
    ] != "encounter_record"
    with pytest.raises(LookupError):
        await e.catalog.world(
            uuid4(), e.ctx.source_timeline_id, e.ctx.source_head_node_id
        )


@pytest.mark.asyncio
async def test_explicit_encounter_uses_world_entity_id_and_requires_own_source(env):
    e = env
    person = uuid4()
    relation = uuid4()
    await e.db.execute(
        "INSERT INTO aios.world_entity VALUES($1,$2,'person','Bob')",
        person,
        e.ctx.world_id,
    )
    meta = json.dumps({"source_node_id": str(e.ctx.source_head_node_id)})
    await e.db.execute(
        "INSERT INTO aios.character_relationship(relationship_id,observer_instance_id,target_entity_id,relationship_type,meta) VALUES($1,$2,$3,'met',$4::jsonb)",
        relation,
        e.ctx.instance_id,
        person,
        meta,
    )
    result = await e.catalog.character(e.ctx.instance_id)
    link = next(r for r in result["familiarity"] if r["mode"] == "encounter_record")
    assert link["entity_id"] == world_entity_iri(person)
    await e.db.execute(
        "UPDATE aios.character_relationship SET observer_instance_id=$2 WHERE relationship_id=$1",
        relation,
        uuid4(),
    )
    assert not any(
        r["mode"] == "encounter_record"
        for r in (await e.catalog.character(e.ctx.instance_id))["familiarity"]
    )


@pytest.mark.asyncio
async def test_corpus_exposure_needs_current_grant_and_matching_content(env):
    e = env
    p = payload()
    section = uuid4()
    document = uuid4()
    research = uuid4()
    text = "Corpus about Bob."
    p.update(
        section_id=str(section), source_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    await e.db.execute("INSERT INTO aios.corpus_document VALUES($1)", document)
    await e.db.execute(
        "INSERT INTO aios.corpus_section VALUES($1,$2,$3)", section, document, text
    )
    await e.db.execute(
        "INSERT INTO aios.corpus_reference_projection(section_id,payload) VALUES($1,$2::jsonb)",
        section,
        json.dumps(p),
    )
    await e.db.execute(
        "INSERT INTO aios.character_research_event VALUES($1,$2)",
        research,
        e.ctx.instance_id,
    )
    await e.db.execute(
        "INSERT INTO aios.character_corpus_exposure(research_id,section_id) VALUES($1,$2)",
        research,
        section,
    )
    await e.db.execute(
        "INSERT INTO aios.corpus_document_scope VALUES($1,'reference.test')", document
    )
    before = await e.catalog.character(e.ctx.instance_id)
    assert not any(
        r["mode"] == "corpus_reference_exposure" for r in before["familiarity"]
    )
    await e.db.execute(
        "INSERT INTO aios.character_corpus_access VALUES('Renamon','reference',true)"
    )
    allowed = await e.catalog.character(e.ctx.instance_id)
    assert any(r["mode"] == "corpus_reference_exposure" for r in allowed["familiarity"])
    await e.db.execute(
        "INSERT INTO aios.character_corpus_access VALUES('Renamon','reference.test',false)"
    )
    assert not any(
        r["mode"] == "corpus_reference_exposure"
        for r in (await e.catalog.character(e.ctx.instance_id))["familiarity"]
    )


@pytest.mark.asyncio
async def test_projection_backfill_requeues_only_rdf_receipts(env):
    e = env
    await e.db.pool.con.query(
        (ROOT / "migrations/current/20261028_01_place_catalog_rdf.sql").read_text(),
        exec=True,
    )
    row = await e.db.fetchrow(
        "SELECT revision,built_revision,rdf_revision FROM aios.entity_memory_projection WHERE section_id=$1",
        e.section,
    )
    assert row["revision"] == row["built_revision"] and row["rdf_revision"] == 0


def test_operator_api_exports_json_and_rdf_and_rejects_invalid_formats(monkeypatch):
    from aios_app.entity_memory import catalog_api

    calls = []

    class Service:
        def __init__(self, db):
            pass

        async def character(self, instance):
            calls.append(instance)
            return {
                "view": "character",
                "graph": "urn:test",
                "entities": [],
                "places": [],
                "familiarity": [],
                "truncated": False,
            }

    monkeypatch.setattr(catalog_api, "EntityCatalogService", Service)
    app = FastAPI()
    app.include_router(catalog_router(None))
    client = TestClient(app)
    url = "/instance/" + str(uuid4()) + "/entity-catalog"
    assert client.get(url).json()["view"] == "character"
    rdf = client.get(url + "?format=trig")
    assert rdf.status_code == 200 and rdf.headers["content-type"].startswith(
        "application/trig"
    )
    assert client.get(url + "?format=invalid").status_code == 422
    assert len(calls) == 2


def test_place_categories_do_not_create_global_location_singletons():
    p = payload()
    room = p["anchors"][0]["entity_id"]
    p["mentions"] = [
        {
            "entity_id": room,
            "head": "bedroom",
            "text": "Mia’s bedroom",
            "start": 0,
            "end": 13,
        }
    ]
    view = reference_view(p)
    assert view["places"][0]["place"]["category"] == "bedroom"
    graph = Dataset()
    graph.update(rdf_update(p["section_id"], p))
    assert list(
        graph.quads(
            (
                None,
                URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
                URIRef(NS + "Bedroom"),
                None,
            )
        )
    )
    assert view["places"][0]["place"]["id"] != "urn:aios:catalog#Bedroom"


@pytest.mark.asyncio
async def test_acquired_corpus_reference_is_distinct_from_exposure_and_revalidated(env):
    e = env
    p = payload()
    section = uuid4()
    document = uuid4()
    text = "Bob lives in a house."
    p.update(
        section_id=str(section), source_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    await e.db.execute("INSERT INTO aios.corpus_document VALUES($1)", document)
    await e.db.execute(
        "INSERT INTO aios.corpus_section VALUES($1,$2,$3)", section, document, text
    )
    await e.db.execute(
        "INSERT INTO aios.corpus_reference_projection(section_id,payload) VALUES($1,$2::jsonb)",
        section,
        json.dumps(p),
    )
    await e.db.execute(
        "INSERT INTO aios.corpus_document_scope VALUES($1,'reference.test')", document
    )
    await e.db.execute(
        "INSERT INTO aios.character_corpus_access VALUES('Renamon','reference',true)"
    )
    consumption, acquisition = uuid4(), uuid4()
    await e.db.execute(
        "INSERT INTO aios.source_consumption(consumption_id,section_id,instance_id,status) VALUES($1,$2,$3,'ingested')",
        consumption,
        section,
        e.ctx.instance_id,
    )
    await e.db.execute(
        "INSERT INTO aios.knowledge_acquisition_event(acquisition_id,instance_id,consumption_id) VALUES($1,$2,$3)",
        acquisition,
        e.ctx.instance_id,
        consumption,
    )
    await e.db.execute(
        "INSERT INTO aios.epistemic_authority_admission VALUES($1,'attested','corpus','reference',ARRAY['reflection'])",
        acquisition,
    )
    await e.db.execute(
        "INSERT INTO aios.semantic_evidence_admission VALUES($1,'active')", acquisition
    )
    catalog = await e.catalog.character(e.ctx.instance_id)
    assert any(r["mode"] == "acquired_corpus_reference" for r in catalog["familiarity"])
    assert not any(
        r["mode"] == "corpus_reference_exposure" for r in catalog["familiarity"]
    )
    await e.db.execute(
        "UPDATE aios.semantic_evidence_admission SET status='revoked' WHERE acquisition_id=$1",
        acquisition,
    )
    assert not any(
        r["mode"] == "acquired_corpus_reference"
        for r in (await e.catalog.character(e.ctx.instance_id))["familiarity"]
    )


@pytest.mark.asyncio
async def test_closed_world_relation_is_history_and_nearness_is_not_containment(env):
    e = env
    room, item = uuid4(), uuid4()
    closed = uuid4()
    await e.db.execute(
        "INSERT INTO aios.world_entity VALUES($1,$2,'room','Store')",
        room,
        e.ctx.world_id,
    )
    await e.db.execute(
        "INSERT INTO aios.world_entity VALUES($1,$2,'object','Merchandise')",
        item,
        e.ctx.world_id,
    )
    await e.db.execute(
        "INSERT INTO aios.world_entity_relation(relation_id,world_id,subject_entity_id,object_entity_id,relation_type,valid_from_node_id,valid_to_node_id) VALUES($1,$2,$3,$4,$5,$6,$6)",
        closed,
        e.ctx.world_id,
        item,
        room,
        "located_in",
        e.ctx.source_head_node_id,
    )
    assert not (
        await e.catalog.world(
            e.ctx.world_id, e.ctx.source_timeline_id, e.ctx.source_head_node_id
        )
    )["places"]
    await e.db.execute(
        "UPDATE aios.world_entity_relation SET relation_type='near',valid_to_node_id=NULL WHERE relation_id=$1",
        closed,
    )
    place = (
        await e.catalog.world(
            e.ctx.world_id, e.ctx.source_timeline_id, e.ctx.source_head_node_id
        )
    )["places"][0]
    assert (
        not place["contents"]
        and place["other_evidence"][0]["entity"]["label"] == "Merchandise"
    )


@pytest.mark.asyncio
async def test_catalog_sql_snapshot_rejects_writes(env, monkeypatch):
    e = env
    original = e.db.fetch

    async def attempted_write(sql, *args):
        await e.db.execute("DELETE FROM aios.entity_memory_projection")
        return await original(sql, *args)

    monkeypatch.setattr(e.db, "fetch", attempted_write)
    with pytest.raises(RuntimeError, match="read-only"):
        await e.catalog.character(e.ctx.instance_id)
    assert (
        await e.db.fetchval("SELECT count(*) FROM aios.entity_memory_projection") == 1
    )


def test_catalog_api_timeout_is_unavailable_not_empty_world(monkeypatch):
    from aios_app.entity_memory import catalog_api

    class SlowService:
        def __init__(self, db):
            pass

        async def world(self, *args):
            raise TimeoutError()

    monkeypatch.setattr(catalog_api, "EntityCatalogService", SlowService)
    app = FastAPI()
    app.include_router(catalog_router(None))
    client = TestClient(app)
    path = f"/world/{uuid4()}/entity-catalog"
    assert client.get(path).status_code == 422
    response = client.get(
        path, params={"timeline_id": str(uuid4()), "head_node_id": str(uuid4())}
    )
    assert response.status_code == 503 and "unknown" in response.json()["detail"]


@pytest.mark.parametrize(
    "contexts",
    [
        [{"character_instance_id": str(uuid4())}, {"character_instance_id": None}],
        [
            {"character_instance_id": str(uuid4())},
            {"character_instance_id": str(uuid4())},
        ],
    ],
)
def test_mixed_source_observers_do_not_gain_all_entity_references(contexts):
    p = payload()
    p["contexts"] = contexts
    graph = Dataset()
    graph.update(rdf_update(p["section_id"], p))
    assert not list(graph.quads((None, URIRef(NS + "hasEntityReference"), None, None)))


def test_nested_places_and_hedges_use_the_phrase_head_and_clause_evidence():
    from aios_app.entity_memory.extraction import extract_memory

    nlp = pytest.importorskip("en_core_web_sm").load()
    text = "Jon walked into the kitchen of Garfield's house in San Francisco."
    data = extract_memory(nlp(text), scope="story", section_id="1")
    assert {a["label"] for a in data["anchors"] if a["kind"] == "place"} == {
        "the kitchen", "Garfield's house", "San Francisco",
    }
    view = reference_view(dict(data, section_id="1"))
    parents = {place["place"]["label"]: [edge["entity"]["label"] for edge in place["contents"]]
               for place in view["places"]}
    assert parents["Garfield's house"] == ["the kitchen"]
    assert parents["San Francisco"] == ["Garfield's house"]
    assert len(data["movement_destinations"]) == 1
    data = extract_memory(nlp("Jon walked into Central Park. The park is busy."),
                          scope="story", section_id="park")
    places = {anchor["label"]: anchor["place_reference"]
              for anchor in data["anchors"] if anchor["kind"] == "place"}
    assert places == {"Central Park": "named", "The park": "definite"}

    for text in (
        "Maybe the kitchen is in Garfield's house.",
        '"The kitchen is in Garfield\'s house."',
        "Jon said the kitchen is in Garfield's house.",
    ):
        data = extract_memory(nlp(text), scope="story", section_id="2")
        view = reference_view(dict(data, section_id="2"))
        assert not any(place["contents"] for place in view["places"])
        assert not any(a.get("place_usable") for a in data["anchors"])


def test_competing_kitchens_require_a_parent_and_an_explicit_identity_can_add_a_name():
    from aios_app.entity_memory.extraction import extract_memory
    from aios_app.entity_memory.place_resolution import resolve_places

    nlp = pytest.importorskip("en_core_web_sm").load()

    def source(index, text):
        section = str(index)
        return dict(extract_memory(nlp(text), scope="story", section_id=section),
                    section_id=section)

    bare = source(1, "Jon walked into the kitchen.")
    explicit = source(2, "The kitchen is Garfield's kitchen.")
    other = source(3, "Jon's kitchen is in Jon's house.")
    ambiguous = source(4, "The kitchen is warm.")
    in_other = source(5, "The kitchen is in Jon's house.")
    bindings, receipts = resolve_places([bare, explicit, other, ambiguous, in_other])

    def kitchen_ids(payload):
        return [a["entity_id"] for a in payload["anchors"] if a.get("place_head") == "kitchen"]

    original, = kitchen_ids(bare)
    assert {bindings["2"][anchor] for anchor in kitchen_ids(explicit)} == {original}
    other_id, = kitchen_ids(other)
    assert other_id != original
    ambiguous_id, = kitchen_ids(ambiguous)
    assert bindings["4"][ambiguous_id] == ambiguous_id
    current, = kitchen_ids(in_other)
    assert bindings["5"][current] == other_id
    assert any(receipt.get("evidence", "").endswith(":relation:0") for receipt in receipts)


@pytest.mark.asyncio
async def test_catalog_resolves_later_place_context_at_the_selected_head(env):
    from dataclasses import replace

    from aios_app.entity_memory.service import collect_entity_memory_once, project_entity_memory_once
    from aios_app.hud.context import HUDContext
    from test_entity_memory_postgres import seed

    nlp = pytest.importorskip("en_core_web_sm").load()
    e = env
    source_timeline = e.ctx.source_timeline_id
    runtime_timeline = uuid4()

    async def add_turn(event, text, parent=None):
        section, _ = await seed(e.db, event=event, doc=nlp(text))
        node = await e.db.fetchval("SELECT node_id FROM aios.document_section WHERE section_id=$1", section)
        await e.db.execute("UPDATE aios.dag_node SET timeline_id=$2 WHERE node_id=$1", node, source_timeline)
        if parent:
            await e.db.execute("INSERT INTO aios.dag_edge VALUES($1,$2,$3)", source_timeline, parent, node)
        return node

    first = await add_turn(2, "Jon walked into the kitchen.")
    second = await add_turn(3, "The kitchen is in Garfield's house.", first)
    head = await add_turn(4, "Garfield's house is in San Francisco.", second)
    sibling = await add_turn(5, "Garfield's house is in New York.", second)
    assert await collect_entity_memory_once(e.db) == 4
    assert await project_entity_memory_once(e.db, e.graphs, limit=4) == 4

    e.ctx = HUDContext(
        instance_id=e.ctx.instance_id, character_id=e.ctx.character_id, entity_id=uuid4(),
        world_id=e.ctx.world_id, world_key="story", timeline_id=runtime_timeline,
        head_node_id=None, source_timeline_id=source_timeline, source_head_node_id=head,
        state_version=1, lifecycle_state="ready", location_entity_id=None,
        lineage_world_ids=(), lineage_instance_ids=(e.ctx.instance_id,), scene_entity_ids=frozenset(),
    )
    await e.db.execute("""CREATE TABLE aios.semantic_topology_node(
        node_key text,dag_node_id uuid,scope_key text,node_type text,meta jsonb)""")
    event = str(uuid4())
    scope = f"world:{e.ctx.world_id}:timeline:{runtime_timeline}:narrated-events"
    await e.db.execute(
        """INSERT INTO aios.semantic_topology_node VALUES($1,$2,$3,'NARRATED_SCENE_EVENT',
           '{"movement":true,"object_label":"the kitchen","action":"walk"}')""", event, first, scope,
    )
    earlier = await e.catalog.character(e.ctx.instance_id, head_node_id=first)
    current = await e.catalog.character(e.ctx.instance_id)
    kitchen, = earlier["places"]
    kitchen_id = kitchen["place"]["id"]
    assert kitchen["place"]["label"] == "the kitchen"
    assert not earlier["place_bindings"]
    places = {p["place"]["label"].lower(): p for p in current["places"]}
    assert places["the kitchen"]["place"]["id"] == kitchen_id
    assert places["garfield's house"]["contents"][0]["entity"]["id"] == kitchen_id
    assert places["san francisco"]["contents"][0]["entity"]["id"] == places["garfield's house"]["place"]["id"]
    assert "new york" not in places
    with pytest.raises(LookupError):
        await e.catalog.character(e.ctx.instance_id, head_node_id=sibling)
    alternative = await e.catalog.character(e.ctx.instance_id, context=replace(e.ctx, source_head_node_id=sibling))
    assert {p["place"]["label"].lower() for p in alternative["places"]} == {
        "the kitchen", "garfield's house", "new york",
    }
    graph = Dataset()
    graph.parse(data=catalog_trig(current), format="trig")
    assert list(graph.quads((URIRef("urn:aios:scene-event:" + event),
                             URIRef(NS + "reportedDestination"), URIRef(kitchen_id), None)))
    results = list(graph.query(f"""
        PREFIX c: <{NS}>
        SELECT ?city WHERE {{ GRAPH ?view {{
          <urn:aios:scene-event:{event}> c:reportedDestination ?room .
          ?house c:hasMembershipEvidence [c:member ?room; c:membershipStatus "reported"] .
          ?city c:hasMembershipEvidence [c:member ?house; c:membershipStatus "reported"] .
        }} }}
    """))
    assert [str(row.city) for row in results] == [places["san francisco"]["place"]["id"]]

    await e.db.execute("UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=3")
    withdrawn = await e.catalog.character(e.ctx.instance_id)
    house = next(p for p in withdrawn["places"] if p["place"]["label"] == "Garfield's house")
    assert not house["contents"]
