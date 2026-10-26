"""Exercise queue triggers, isolation, receipts and retraction in real PostgreSQL."""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from aios_app.db import Database
from aios_app.entity_memory.service import collect_entity_memory_once, project_entity_memory_once
from test_entity_memory import scene_doc

pytestmark = pytest.mark.skipif(not os.getenv("AIOS_PGLITE_MODULE"), reason="PGlite not configured")
ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = '''
CREATE SCHEMA aios;
CREATE TABLE aios.document_section(section_id uuid PRIMARY KEY,node_id uuid,content text);
CREATE TABLE aios.linguistic_projection(section_id uuid PRIMARY KEY REFERENCES aios.document_section ON DELETE CASCADE,
 doc_json jsonb,content_sha256 text,parser_name text,parser_version text);
CREATE TABLE aios.dag_node(node_id uuid PRIMARY KEY,timeline_id uuid,event_id bigint,character_id text,
 kind text,speaker_id text,recipient_id text);
CREATE TABLE aios.timeline(timeline_id uuid PRIMARY KEY,world_id uuid);
CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY,superseded_at timestamptz);
CREATE TABLE aios.extracted_sentence(sentence_id uuid PRIMARY KEY,section_id uuid,sentence_index int);
CREATE TABLE aios.claim_candidate(claim_id uuid PRIMARY KEY,sentence_id uuid);
CREATE TABLE aios.claim_context_resolution(claim_id uuid PRIMARY KEY,origin_character_id text,
 epistemic_scope text,acquisition_mode text,character_instance_id uuid,viewpoint_id text,source_kind text);
CREATE TABLE aios.character_identity(character_id text PRIMARY KEY,display_name text,canonical_name text);
CREATE TABLE aios.character_alias(character_id text,alias text);
'''


def encode(value):
    if isinstance(value, UUID): return str(value)
    if isinstance(value,(list,tuple)): return [encode(v) for v in value]
    return value


class Connection:
    def __init__(self, process): self.process=process; self.depth=0
    async def query(self,sql,args=(),exec=False):
        self.process.stdin.write((json.dumps(dict(sql=sql,args=encode(args),exec=exec))+"\n").encode())
        await self.process.stdin.drain()
        response=json.loads(await self.process.stdout.readline())
        if "error" in response: raise RuntimeError(response["error"])
        return response["value"]
    async def fetch(self,sql,*args):
        result=await self.query(sql,args)
        for row in result["rows"]:
            for field in result["fields"]:
                if field["dataTypeID"]==2950 and row[field["name"]]:
                    row[field["name"]]=UUID(row[field["name"]])
        return result["rows"]
    async def fetchrow(self,sql,*args):
        rows=await self.fetch(sql,*args); return rows[0] if rows else None
    async def fetchval(self,sql,*args):
        row=await self.fetchrow(sql,*args); return next(iter(row.values())) if row else None
    async def execute(self,sql,*args): return await self.query(sql,args)
    @asynccontextmanager
    async def transaction(self):
        await self.query("BEGIN")
        try: yield
        except BaseException:
            await self.query("ROLLBACK"); raise
        else: await self.query("COMMIT")


class Pool:
    def __init__(self,con): self.con=con
    @asynccontextmanager
    async def acquire(self): yield self.con


@pytest_asyncio.fixture
async def env():
    process=await asyncio.create_subprocess_exec("node",str(ROOT/"tests/cognition_pglite_bridge.mjs"),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
    con=Connection(process)
    await con.query(BOOTSTRAP,exec=True)
    await con.query((ROOT/"migrations/current/20261020_01_entity_memory.sql").read_text(),exec=True)
    db=Database("isolated"); db.pool=Pool(con)
    try: yield db
    finally:
        process.stdin.close(); await process.wait()


async def seed(db, *, owner="Renamon", scope="character", acquisition="character_utterance", event=1, doc=None):
    import hashlib
    section,node,timeline,world,sentence,claim=[uuid4() for _ in range(6)]
    doc=doc if doc is not None else scene_doc()
    await db.execute("INSERT INTO aios.ingest_event VALUES($1,NULL)",event)
    await db.execute("INSERT INTO aios.timeline VALUES($1,$2)",timeline,world)
    await db.execute("INSERT INTO aios.dag_node VALUES($1,$2,$3,$4,'chat_message','Rogue','Renamon')",node,timeline,event,owner)
    await db.execute("INSERT INTO aios.document_section VALUES($1,$2,$3)",section,node,doc.text)
    await db.execute("INSERT INTO aios.linguistic_projection VALUES($1,$2::jsonb,$3,'spacy','en_core_web_sm')",
                     section,json.dumps(doc.to_json()),hashlib.sha256(doc.text.encode()).hexdigest())
    await db.execute("INSERT INTO aios.extracted_sentence VALUES($1,$2,0)",sentence,section)
    await db.execute("INSERT INTO aios.claim_candidate VALUES($1,$2)",claim,sentence)
    await db.execute("INSERT INTO aios.claim_context_resolution VALUES($1,$2,$3,$4,NULL,$2,'chat')",claim,owner,scope,acquisition)
    return section,claim


class Fuseki:
    def __init__(self,fail=False): self.writes=[]; self.fail=fail
    def update(self,dataset,query):
        if self.fail: raise RuntimeError("Fuseki unavailable")
        self.writes.append((dataset,query))


@pytest.mark.asyncio
async def test_cached_backfill_identity_override_and_idempotent_receipts(env):
    db=env
    section,_=await seed(db)
    await db.execute("INSERT INTO aios.character_identity VALUES('Rogue_001','Rogue','Rogue')")
    assert await collect_entity_memory_once(db)==1
    payload=await db.fetchval("SELECT payload FROM aios.entity_memory_projection WHERE section_id=$1",section)
    assert payload["anchors"][0]["character_id"]=="Rogue_001"
    assert payload["anchors"][0]["kind"]=="person"
    assert await collect_entity_memory_once(db)==0
    fuseki=Fuseki()
    assert await project_entity_memory_once(db,fuseki)==1
    assert fuseki.writes[-1][0]=="char"
    assert await project_entity_memory_once(db,fuseki)==0
    assert len(fuseki.writes)==2


@pytest.mark.asyncio
async def test_failure_never_acknowledges_and_supersession_retracts(env):
    db=env
    section,_=await seed(db)
    await collect_entity_memory_once(db)
    with pytest.raises(RuntimeError,match="unavailable"):
        await project_entity_memory_once(db,Fuseki(fail=True))
    assert await db.fetchval("SELECT rdf_revision FROM aios.entity_memory_projection WHERE section_id=$1",section)==0
    fuseki=Fuseki()
    assert await project_entity_memory_once(db,fuseki)==1
    await db.execute("UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=1")
    assert await collect_entity_memory_once(db)==1
    assert await project_entity_memory_once(db,fuseki)==1
    assert fuseki.writes[-1][0]=="char"
    assert "hasAnchor" not in fuseki.writes[-1][1]
    assert await project_entity_memory_once(db,fuseki)==0


@pytest.mark.asyncio
async def test_ownership_move_clears_old_dataset_and_corpus_cannot_bind_runtime_identity(env):
    db=env
    section,claim=await seed(db,owner=None,scope="world",acquisition="source_document")
    await db.execute("INSERT INTO aios.character_identity VALUES('Rogue_001','Rogue','Rogue')")
    await collect_entity_memory_once(db)
    payload=await db.fetchval("SELECT payload FROM aios.entity_memory_projection WHERE section_id=$1",section)
    assert all(x["character_id"] is None and x["participant_id"] is None for x in payload["anchors"])
    fuseki=Fuseki()
    await project_entity_memory_once(db,fuseki)
    assert fuseki.writes[-1][0]=="world"
    await db.execute("UPDATE aios.claim_context_resolution SET origin_character_id='Renamon',epistemic_scope='character' WHERE claim_id=$1",claim)
    await collect_entity_memory_once(db)
    await project_entity_memory_once(db,fuseki)
    assert [dataset for dataset,_ in fuseki.writes]==["char","world","world","char"]
    assert "hasAnchor" not in fuseki.writes[2][1]


@pytest.mark.asyncio
async def test_missing_context_defers_and_deleted_source_retains_retraction_tombstone(env):
    db=env
    section,claim=await seed(db)
    await db.execute("DELETE FROM aios.claim_context_resolution WHERE claim_id=$1",claim)
    assert await collect_entity_memory_once(db)==0
    assert await project_entity_memory_once(db,Fuseki())==0
    await db.execute("INSERT INTO aios.claim_context_resolution VALUES($1,'Renamon','character','character_utterance',NULL,NULL,'chat')",claim)
    assert await collect_entity_memory_once(db)==1
    fuseki=Fuseki()
    await project_entity_memory_once(db,fuseki)
    await db.execute("DELETE FROM aios.document_section WHERE section_id=$1",section)
    assert await collect_entity_memory_once(db)==1
    assert await project_entity_memory_once(db,fuseki)==1
    assert "hasAnchor" not in fuseki.writes[-1][1]


@pytest.mark.asyncio
async def test_identity_alias_edits_invalidate_only_matching_sources(env):
    db=env
    first,_=await seed(db)
    second,_=await seed(db,event=2,doc=scene_doc(name="Bob"))
    assert await collect_entity_memory_once(db)==2
    await db.execute("INSERT INTO aios.character_identity VALUES('Anna','Anna','Anna')")
    assert await collect_entity_memory_once(db)==0
    await db.execute("INSERT INTO aios.character_alias VALUES('Anna','Rogue')")
    assert await collect_entity_memory_once(db)==1
    payload=await db.fetchval("SELECT payload FROM aios.entity_memory_projection WHERE section_id=$1",first)
    assert payload["anchors"][0]["character_id"]=="Anna"
    row=await db.fetchrow("SELECT revision,built_revision FROM aios.entity_memory_projection WHERE section_id=$1",second)
    assert row["revision"]==row["built_revision"]
    await db.execute("DELETE FROM aios.character_alias WHERE alias='Rogue'")
    assert await collect_entity_memory_once(db)==1
    payload=await db.fetchval("SELECT payload FROM aios.entity_memory_projection WHERE section_id=$1",first)
    assert payload["anchors"][0]["character_id"] is None


@pytest.mark.asyncio
async def test_source_reparse_replaces_mentions_without_leaving_old_anchors(env):
    import hashlib
    db=env
    section,_=await seed(db)
    await collect_entity_memory_once(db)
    fuseki=Fuseki()
    await project_entity_memory_once(db,fuseki)
    replacement=scene_doc(name="Bob")
    await db.execute("UPDATE aios.document_section SET content=$2 WHERE section_id=$1",section,replacement.text)
    await db.execute("UPDATE aios.linguistic_projection SET doc_json=$2::jsonb,content_sha256=$3 WHERE section_id=$1",
                     section,json.dumps(replacement.to_json()),hashlib.sha256(replacement.text.encode()).hexdigest())
    assert await collect_entity_memory_once(db)==1
    assert await project_entity_memory_once(db,fuseki)==1
    assert '"Bob"' in fuseki.writes[-1][1]
    assert '"Rogue"' not in fuseki.writes[-1][1]


@pytest.mark.asyncio
async def test_lost_context_withdraws_already_published_graph(env):
    db=env
    section,claim=await seed(db,owner=None,scope="world")
    await collect_entity_memory_once(db)
    fuseki=Fuseki()
    await project_entity_memory_once(db,fuseki)
    await db.execute("DELETE FROM aios.claim_context_resolution WHERE claim_id=$1",claim)
    assert await collect_entity_memory_once(db)==1
    assert await project_entity_memory_once(db,fuseki)==1
    assert [d for d,_ in fuseki.writes[-2:]]==["world","char"]
    assert all("hasAnchor" not in q for _,q in fuseki.writes[-2:])
    await db.execute("INSERT INTO aios.claim_context_resolution VALUES($1,'Renamon','character','character_utterance',NULL,NULL,'chat')",claim)
    assert await collect_entity_memory_once(db)==1
    assert await project_entity_memory_once(db,fuseki)==1
    assert fuseki.writes[-1][0]=="char"
    assert "hasAnchor" in fuseki.writes[-1][1]


@pytest.mark.asyncio
async def test_bad_cached_parse_does_not_starve_other_sections(env):
    db=env
    bad,_=await seed(db)
    good,_=await seed(db,event=2)
    await db.execute("UPDATE aios.linguistic_projection SET doc_json='{}' WHERE section_id=$1",bad)
    assert await collect_entity_memory_once(db)==1
    assert await db.fetchval("SELECT built_revision=revision FROM aios.entity_memory_projection WHERE section_id=$1",good)
    assert await db.fetchval("SELECT retry_after>now() FROM aios.entity_memory_projection WHERE section_id=$1",bad)


@pytest.mark.asyncio
async def test_lost_sql_ack_followed_by_source_deletion_clears_external_graph(env, monkeypatch):
    from rdflib import Dataset
    db=env
    section,_=await seed(db)
    await collect_entity_memory_once(db)
    external={"char":Dataset(),"world":Dataset()}
    class External:
        def update(self,dataset,query): external[dataset].update(query)
    original=db.execute
    async def lose_ack(sql,*args):
        if "SET rdf_revision=built_revision" in sql:
            raise RuntimeError("SQL receipt lost")
        return await original(sql,*args)
    monkeypatch.setattr(db,"execute",lose_ack)
    with pytest.raises(RuntimeError,match="receipt lost"):
        await project_entity_memory_once(db,External())
    assert len(external["char"])>0
    monkeypatch.setattr(db,"execute",original)
    await db.execute("DELETE FROM aios.document_section WHERE section_id=$1",section)
    await collect_entity_memory_once(db)
    assert await project_entity_memory_once(db,External())==1
    assert len(external["char"])==len(external["world"])==0


@pytest.mark.asyncio
async def test_equivalent_context_refresh_does_not_repeat_rdf_io(env):
    db=env
    section,claim=await seed(db)
    await collect_entity_memory_once(db)
    fuseki=Fuseki()
    await project_entity_memory_once(db,fuseki)
    await db.execute("UPDATE aios.claim_context_resolution SET epistemic_scope=epistemic_scope WHERE claim_id=$1",claim)
    assert await collect_entity_memory_once(db)==1
    assert await project_entity_memory_once(db,fuseki)==0
    assert len(fuseki.writes)==2
