"""Corpus RDF remains reference-only across parsing, edits, routing and retries."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from aios_app.corpus_rdf.extraction import build_payload, role_spans
from aios_app.corpus_rdf.service import (
    NS,
    _parse,
    collect_corpus_references_once,
    graph_iri,
    project_corpus_references_once,
    rdf_update,
)
from aios_app.db import Database
from aios_app.entity_memory.service import cached_doc
from rdflib import Dataset, URIRef
from test_entity_memory_postgres import Connection, Pool

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = """
CREATE SCHEMA aios;
CREATE TABLE aios.corpus_document(document_id uuid PRIMARY KEY, title text, source_id text,
 source_uri text, document_kind text,language text,meta jsonb DEFAULT '{}',
 epistemic_namespace text,identity_binding text DEFAULT 'external');
CREATE TABLE aios.corpus_section(section_id uuid PRIMARY KEY,document_id uuid REFERENCES aios.corpus_document ON DELETE CASCADE,
 content text,heading text,meta jsonb DEFAULT '{}');
CREATE TABLE aios.corpus_document_domain(document_id uuid REFERENCES aios.corpus_document ON DELETE CASCADE,knowledge_domain text);
CREATE TABLE aios.corpus_document_scope(document_id uuid REFERENCES aios.corpus_document ON DELETE CASCADE,scope_key text);
CREATE TABLE aios.corpus_document_facet(document_id uuid REFERENCES aios.corpus_document ON DELETE CASCADE,facet_type text,facet_value text);
CREATE TABLE aios.knowledge_domain(domain_id uuid PRIMARY KEY,domain_key text,display_name text,domain_kind text,
 parent_domain_id uuid REFERENCES aios.knowledge_domain ON DELETE SET NULL, enabled boolean DEFAULT true,default_epistemic_namespace text);
"""


@pytest_asyncio.fixture
async def db():
    if not os.getenv("AIOS_PGLITE_MODULE"):
        pytest.skip("PGlite not configured")
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "tests/cognition_pglite_bridge.mjs"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    con = Connection(process)
    await con.query(BOOTSTRAP, exec=True)
    await con.query(
        (ROOT / "migrations/current/20261024_01_corpus_reference_rdf.sql").read_text(),
        exec=True,
    )
    database = Database("isolated")
    database.pool = Pool(con)
    try:
        yield database
    finally:
        process.stdin.close()
        await process.wait()


class Fuseki:
    def __init__(self):
        self.graph = Dataset()
        self.calls = []
        self.fail = False

    def update(self, dataset, query):
        assert dataset == "world"
        if self.fail:
            raise RuntimeError("offline")
        self.calls.append(query)
        self.graph.update(query)


async def seed(
    db, *, namespace="canon.magic", text="Ms. Frizzle has a school bus.", meta=None
):
    document, section = uuid4(), uuid4()
    await db.execute(
        "INSERT INTO aios.corpus_document(document_id,title,epistemic_namespace,meta) VALUES($1,$2,$3,$4::jsonb)",
        document,
        "Reference",
        namespace,
        json.dumps(meta or {}),
    )
    await db.execute(
        "INSERT INTO aios.corpus_section(section_id,document_id,content) VALUES($1,$2,$3)",
        section,
        document,
        text,
    )
    return document, section


def row(
    *,
    section=None,
    document=None,
    namespace="canon.magic",
    text="Ms. Frizzle has a school bus.",
    meta=None,
):
    return {
        "section_id": section or uuid4(),
        "document_id": document or uuid4(),
        "content": text,
        "section_meta": meta or {},
        "document_meta": {},
        "epistemic_namespace": namespace,
        "identity_binding": "external",
        "domains": [],
        "scopes": [],
    }


def test_names_repeat_only_in_document_namespace():
    document = uuid4()
    first = row(document=document)
    second = row(document=document)
    doc = cached_doc(_parse(first["content"]))

    def person(r):
        p = build_payload(r, doc)
        return next(a["entity_id"] for a in p["anchors"] if "Frizzle" in a["label"])

    assert person(first) == person(second)
    assert person(first) != person(row())
    assert person(first) != person({**first, "epistemic_namespace": "other.universe"})
    assert all(a["character_id"] is None for a in build_payload(first, doc)["anchors"])


def test_dictionary_examples_have_local_entities_and_qualified_relations():
    text = "Definition: A foolish person.\nExample: Bob has a red car.\nExample: Bob has a blue car."
    r = row(namespace="reference.urban", text=text, meta={"term": "goof"})
    r["document_meta"] = {"reference_kind": "dictionary"}
    payload = build_payload(r, cached_doc(_parse(text)))
    assert [s["role"] for s in payload["roles"]] == ["definition", "example", "example"]
    bobs = [m["entity_id"] for m in payload["mentions"] if m["text"] == "Bob"]
    assert len(set(bobs)) == 2
    assert payload["lexical_entry"]["term"] == "goof"
    assert payload["relations"] and all(
        r["authority"] == "illustrative_only" for r in payload["relations"]
    )
    graph = Dataset()
    graph.update(rdf_update(r["section_id"], payload))
    assert list(graph.quads((None, URIRef(NS + "term"), None, None)))
    assert not list(graph.quads((None, URIRef("urn:aios:world#subject"), None, None)))


def test_unmarked_dictionary_is_not_a_world_report():
    r = row(text="Bob has a car.")
    r["source_uri"] = "https://www.urbandictionary.com/define.php?term=test"
    payload = build_payload(r, cached_doc(_parse(r["content"])))
    assert payload["reference_kind"] == "dictionary"
    assert all(m["content_role"] == "lexical_reference" for m in payload["mentions"])
    assert all(r["authority"] == "illustrative_only" for r in payload["relations"])
    assert role_spans("Bob has a car.", heading="Examples")[0]["role"] == "example"


@pytest.mark.asyncio
async def test_projection_queue_receipts_and_metadata_reuse(db):
    document, section = await seed(db)
    parses = []

    def parser(text):
        parses.append(text)
        return _parse(text)

    assert await collect_corpus_references_once(db, parser=parser) == 1
    assert await collect_corpus_references_once(db, parser=parser) == 0
    fuseki = Fuseki()
    assert await project_corpus_references_once(db, fuseki) == 1
    assert await project_corpus_references_once(db, fuseki) == 0
    assert len(parses) == 1
    domain = uuid4()
    await db.execute(
        "INSERT INTO aios.knowledge_domain(domain_id,domain_key,display_name,domain_kind,parent_domain_id,enabled) VALUES($1,'fiction.magic','Magic','fictional_universe',NULL,true)",
        domain,
    )
    await db.execute(
        "INSERT INTO aios.corpus_document_domain VALUES($1,'fiction.magic')", document
    )
    await db.execute(
        "INSERT INTO aios.corpus_document_scope VALUES($1,'magic.reference')", document
    )
    assert await collect_corpus_references_once(db, parser=parser) == 1
    assert len(parses) == 1
    assert await project_corpus_references_once(db, fuseki) == 1
    graph = fuseki.graph.graph(URIRef(graph_iri(section)))
    assert list(graph.triples((None, URIRef(NS + "domain"), None)))
    await db.execute(
        "UPDATE aios.knowledge_domain SET display_name='Magic revised' WHERE domain_id=$1",
        domain,
    )
    assert await collect_corpus_references_once(db, parser=parser) == 1
    assert await project_corpus_references_once(db, fuseki) == 1
    assert "Magic revised" in fuseki.calls[-1]
    assert len(parses) == 1
    # No ingestion, character tables, or grants even exist in this fixture.
    assert (
        await db.fetchval("SELECT to_regclass('aios.knowledge_acquisition_event')")
        is None
    )


@pytest.mark.asyncio
async def test_text_edit_reparses_namespace_edit_does_not(db):
    document, section = await seed(db)
    parses = []

    def parser(text):
        parses.append(text)
        return _parse(text)

    await collect_corpus_references_once(db, parser=parser)
    old = await db.fetchval(
        "SELECT payload FROM aios.corpus_reference_projection WHERE section_id=$1",
        section,
    )
    await db.execute(
        "UPDATE aios.corpus_document SET epistemic_namespace='canon.other' WHERE document_id=$1",
        document,
    )
    await collect_corpus_references_once(db, parser=parser)
    new = await db.fetchval(
        "SELECT payload FROM aios.corpus_reference_projection WHERE section_id=$1",
        section,
    )
    assert old["anchors"][0]["entity_id"] != new["anchors"][0]["entity_id"]
    assert len(parses) == 1
    await db.execute(
        "UPDATE aios.corpus_section SET content='Bob has a car.' WHERE section_id=$1",
        section,
    )
    await collect_corpus_references_once(db, parser=parser)
    assert len(parses) == 2


@pytest.mark.asyncio
async def test_deleted_document_retracts_graph_and_failed_write_keeps_receipt_pending(
    db,
):
    document, section = await seed(db)
    await collect_corpus_references_once(db)
    fuseki = Fuseki()
    fuseki.fail = True
    with pytest.raises(RuntimeError):
        await project_corpus_references_once(db, fuseki)
    assert (
        await db.fetchval(
            "SELECT rdf_revision FROM aios.corpus_reference_projection WHERE section_id=$1",
            section,
        )
        == 0
    )
    fuseki.fail = False
    assert await project_corpus_references_once(db, fuseki) == 1
    await db.execute("DELETE FROM aios.corpus_document WHERE document_id=$1", document)
    assert await collect_corpus_references_once(db) == 1
    assert await project_corpus_references_once(db, fuseki) == 1
    assert len(fuseki.graph.graph(URIRef(graph_iri(section)))) == 0


@pytest.mark.asyncio
async def test_stale_built_graph_not_published_before_rebuild(db):
    _document, section = await seed(db)
    await collect_corpus_references_once(db)
    await db.execute(
        "UPDATE aios.corpus_section SET content='Bob has a car.' WHERE section_id=$1",
        section,
    )
    assert await project_corpus_references_once(db, Fuseki()) == 0
    await collect_corpus_references_once(db)
    assert await project_corpus_references_once(db, Fuseki()) == 1


@pytest.mark.asyncio
async def test_existing_sections_are_queued_by_migration():
    if not os.getenv("AIOS_PGLITE_MODULE"):
        pytest.skip("PGlite not configured")
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "tests/cognition_pglite_bridge.mjs"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    con = Connection(process)
    try:
        await con.query(BOOTSTRAP, exec=True)
        await con.query(
            "INSERT INTO aios.corpus_document(document_id) VALUES('00000000-0000-0000-0000-000000000001'); INSERT INTO aios.corpus_section VALUES('00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001','Bob',NULL,'{}');",
            exec=True,
        )
        await con.query(
            (
                ROOT / "migrations/current/20261024_01_corpus_reference_rdf.sql"
            ).read_text(),
            exec=True,
        )
        assert (
            await con.fetchrow(
                "SELECT count(*) n FROM aios.corpus_reference_projection"
            )
        )["n"] == 1
    finally:
        process.stdin.close()
        await process.wait()


@pytest.mark.asyncio
async def test_unsupported_language_keeps_metadata_without_running_english_parser(db):
    document, section = await seed(db, text="Bonjour Bob.")
    await db.execute(
        "UPDATE aios.corpus_document SET language='fr' WHERE document_id=$1", document
    )

    def forbidden(text):
        raise AssertionError("English parser must not run")

    assert await collect_corpus_references_once(db, parser=forbidden) == 1
    payload = await db.fetchval(
        "SELECT payload FROM aios.corpus_reference_projection WHERE section_id=$1",
        section,
    )
    assert payload["parse_status"] == "unsupported_language"
    assert payload["anchors"] == [] and payload["parsed_chars"] == 0
    assert await project_corpus_references_once(db, Fuseki()) == 1


@pytest.mark.asyncio
async def test_parse_failure_backs_off_without_stalling_other_sources(db):
    _, bad = await seed(db, text="bad parser fixture")
    _, _good = await seed(db, text="Bob has a car.")

    def parser(text):
        if text.startswith("bad"):
            raise ValueError("broken")
        return _parse(text)

    assert await collect_corpus_references_once(db, limit=4, parser=parser) == 1
    work = await db.fetchrow(
        "SELECT built_revision,last_error,retry_after>now() deferred FROM aios.corpus_reference_projection WHERE section_id=$1",
        bad,
    )
    assert (
        work["built_revision"] == 0
        and work["last_error"] == "ValueError"
        and work["deferred"]
    )
    assert await project_corpus_references_once(db, Fuseki()) == 1


def test_source_offsets_escaping_and_bounds():
    from aios_app.corpus_rdf.extraction import MAX_PARSE_CHARS

    text = "Bob has a car. " + "more text " * 2000
    r = row(text=text)
    payload = build_payload(r, cached_doc(_parse(text[:MAX_PARSE_CHARS])))
    assert payload["truncated"] and payload["parsed_chars"] == MAX_PARSE_CHARS
    for m in payload["mentions"]:
        assert text[m["start"] : m["end"]] == m["text"]
    payload["title"] = 'quoted "title" \\ newline\n🦊'
    # Execute small SPARQL lifecycle cases above. For the maximum-size graph,
    # validate the emitted triples directly: RDFLib's INSERT DATA algebra sorts
    # large constant blocks cubically (unlike the production Jena writer).
    from rdflib import Graph

    query = rdf_update(r["section_id"], payload)
    triples = query.split(
        "INSERT DATA { GRAPH <" + graph_iri(r["section_id"]) + "> {\n", 1
    )[1].rsplit("\n} }", 1)[0]
    graph = Graph().parse(data=triples, format="nt")
    assert str(next(graph.objects(None, URIRef(NS + "title")))) == payload["title"]


@pytest.mark.asyncio
async def test_registry_deletion_and_section_move_invalidate(db):
    document, section = await seed(db)
    domain = uuid4()
    await db.execute(
        "INSERT INTO aios.knowledge_domain(domain_id,domain_key,display_name,domain_kind,parent_domain_id,enabled) VALUES($1,'fiction.magic','Magic','fictional_universe',NULL,true)",
        domain,
    )
    await db.execute(
        "INSERT INTO aios.corpus_document_domain VALUES($1,'fiction.magic')", document
    )
    await collect_corpus_references_once(db)
    await db.execute("DELETE FROM aios.knowledge_domain WHERE domain_id=$1", domain)
    assert await collect_corpus_references_once(db) == 1
    payload = await db.fetchval(
        "SELECT payload FROM aios.corpus_reference_projection WHERE section_id=$1",
        section,
    )
    assert payload["domains"][0]["name"] is None
    other, _ = await seed(db, namespace="other")
    await db.execute(
        "UPDATE aios.corpus_section SET document_id=$2 WHERE section_id=$1",
        section,
        other,
    )
    assert await collect_corpus_references_once(db, limit=4) == 2
    payload = await db.fetchval(
        "SELECT payload FROM aios.corpus_reference_projection WHERE section_id=$1",
        section,
    )
    assert payload["document_id"] == str(other) and payload["namespace"] == "other"


@pytest.mark.asyncio
async def test_cancelled_transport_holds_lock_until_write_finishes():
    import threading
    from contextlib import asynccontextmanager

    entered = threading.Event()
    release = threading.Event()
    exited = []
    receipts = []

    class DB:
        @asynccontextmanager
        async def atomic(self):
            try:
                yield
            finally:
                exited.append(True)

        async def fetchrow(self, *args):
            return {"section_id": uuid4(), "payload": {}}

        async def execute(self, *args):
            receipts.append(args)

    class SlowFuseki:
        def update(self, *args):
            entered.set()
            release.wait(timeout=5)

    task = asyncio.create_task(project_corpus_references_once(DB(), SlowFuseki()))
    try:
        await asyncio.to_thread(entered.wait, 2)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not exited
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited and not receipts


@pytest.mark.asyncio
async def test_domain_status_tracks_pending_ready_and_edited_sources(db):
    from aios_app.corpus_rdf.service import corpus_reference_status

    document, _section = await seed(db)
    await db.execute(
        "INSERT INTO aios.corpus_document_domain VALUES($1,'fiction.magic')", document
    )
    status = await corpus_reference_status(db, "fiction.magic")
    assert status["total"] == status["pending"] == 1 and status["ready"] == 0
    await collect_corpus_references_once(db)
    assert (await corpus_reference_status(db, "fiction.magic"))["pending"] == 1
    await project_corpus_references_once(db, Fuseki())
    assert (await corpus_reference_status(db, "fiction.magic"))["ready"] == 1
    await db.execute(
        "UPDATE aios.corpus_document SET title='Revised' WHERE document_id=$1", document
    )
    assert (await corpus_reference_status(db, "fiction.magic"))["ready"] == 0


@pytest.mark.asyncio
async def test_domain_namespace_mismatch_is_reported_without_rewriting_document(db):
    from aios_app.corpus_rdf.service import corpus_reference_status

    document, section = await seed(db, namespace="reference")
    await db.execute(
        "INSERT INTO aios.knowledge_domain(domain_id,domain_key,default_epistemic_namespace) VALUES($1,'fiction.magic','canon.magic')",
        uuid4(),
    )
    await db.execute(
        "INSERT INTO aios.corpus_document_domain VALUES($1,'fiction.magic')", document
    )
    assert (await corpus_reference_status(db, "fiction.magic"))[
        "namespace_mismatch"
    ] == 1
    await collect_corpus_references_once(db)
    payload = await db.fetchval(
        "SELECT payload FROM aios.corpus_reference_projection WHERE section_id=$1",
        section,
    )
    assert payload["namespace"] == "reference"
    assert payload["domains"][0]["default_namespace"] == "canon.magic"
