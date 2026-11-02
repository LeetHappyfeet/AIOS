"""Real revision triggers/discovery plus the production integrity/repair gate."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from aios_app.db import Database
from aios_app.epistemic import normalizer
from aios_app.supervisor import STAGES
from test_cognitive_episode_postgres import PGConnection, PGPool

pytestmark = pytest.mark.skipif(not os.getenv('AIOS_PGLITE_MODULE'),reason='PGlite not configured')
ROOT=Path(__file__).resolve().parents[1]
MIGRATION=ROOT/'migrations/current/20261030_01_normalization_blocked_revisions.sql'
BOOTSTRAP='''
CREATE SCHEMA aios;
CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY,source text,source_id text,source_kind text,superseded_at timestamptz,processed_at timestamptz);
CREATE TABLE aios.dag_node(node_id uuid PRIMARY KEY,event_id bigint,speaker_id text,timeline_id uuid);
CREATE TABLE aios.document_section(section_id uuid PRIMARY KEY,node_id uuid,content text,updated_at timestamptz,claims_extracted_at timestamptz);
CREATE TABLE aios.extracted_sentence(sentence_id uuid PRIMARY KEY,section_id uuid,sentence_text text);
CREATE TABLE aios.claim_candidate(claim_id uuid PRIMARY KEY,sentence_id uuid,raw_text text,created_at timestamptz DEFAULT now());
CREATE TABLE aios.claim_context_resolution(claim_id uuid PRIMARY KEY REFERENCES aios.claim_candidate ON DELETE CASCADE,
 resolver_version text,meta jsonb DEFAULT '{}',resolved_at timestamptz DEFAULT now());
CREATE TABLE aios.claim_semantic_frame(frame_id uuid PRIMARY KEY,claim_id uuid REFERENCES aios.claim_candidate ON DELETE CASCADE,
 frame_index integer DEFAULT 0,decomposer_version text DEFAULT 'semantic-frame-v2',
 subject_text text,resolved_subject text,predicate_surface text,predicate_canonical text,
 object_text text,resolved_object text,polarity integer DEFAULT 1,modality text DEFAULT 'asserted',
 resolution_status text DEFAULT 'partial',meta jsonb DEFAULT '{}',updated_at timestamptz DEFAULT now());
CREATE TABLE aios.claim_semantic_frame_projection(claim_id uuid PRIMARY KEY REFERENCES aios.claim_candidate ON DELETE CASCADE,
 decomposer_version text DEFAULT 'semantic-frame-v2',primary_frame_id uuid,updated_at timestamptz DEFAULT now());
CREATE TABLE aios.observation(observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),claim_id uuid,meta jsonb DEFAULT '{}');
CREATE TABLE aios.observation_proposition(observation_id uuid,frame_id uuid);
CREATE TABLE aios.proposition_evidence(observation_id uuid);
CREATE TABLE aios.pipeline_job(job_type text,status text,payload jsonb);
'''


@pytest_asyncio.fixture
async def env(monkeypatch):
    process=await asyncio.create_subprocess_exec('node',str(ROOT/'tests/cognition_pglite_bridge.mjs'),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
    con=PGConnection(process)
    db=Database('isolated'); db.pool=PGPool(con)
    await con.query(BOOTSTRAP,exec=True)
    await con.query((ROOT/'migrations/current/20261001_claim_semantic_integrity.sql').read_text(),exec=True)
    await con.query('''ALTER TABLE aios.claim_semantic_integrity
        ADD COLUMN source_section_digest text,ADD COLUMN source_section_id uuid,
        ADD COLUMN source_sentence_digest text,ADD COLUMN source_node_id uuid;''',exec=True)
    claim,sentence,section,node,frame=[uuid4() for _ in range(5)]
    await db.execute("INSERT INTO aios.ingest_event(event_id,source) VALUES(1,'test')")
    await db.execute("INSERT INTO aios.dag_node VALUES($1,1,'Renamon',$2)",node,uuid4())
    await db.execute("INSERT INTO aios.document_section(section_id,node_id,content) VALUES($1,$2,'It rests.')",section,node)
    await db.execute("INSERT INTO aios.extracted_sentence VALUES($1,$2,'It rests.')",sentence,section)
    await db.execute("INSERT INTO aios.claim_candidate(claim_id,sentence_id,raw_text) VALUES($1,$2,'It rests.')",claim,sentence)
    await db.execute("INSERT INTO aios.claim_semantic_frame(frame_id,claim_id,subject_text,predicate_canonical) VALUES($1,$2,'it','rest')",frame,claim)
    await db.execute("INSERT INTO aios.claim_semantic_frame_projection(claim_id,primary_frame_id) VALUES($1,$2)",claim,frame)
    await db.execute("INSERT INTO aios.claim_context_resolution(claim_id,resolver_version) VALUES($1,'context-resolver-v4-semantic')",claim)
    # Apply the actual migration over existing data, not a fixture substitute.
    await con.query(MIGRATION.read_text(),exec=True)
    published=[]
    async def publish(db,*,claim_id):
        published.append(claim_id)
        observation=await db.fetchval("INSERT INTO aios.observation(claim_id) VALUES($1) RETURNING observation_id",claim_id)
        await db.execute("INSERT INTO aios.observation_proposition VALUES($1,$2)",observation,frame)
        return uuid4()
    monkeypatch.setattr(normalizer.legacy,'normalize_claim_once',publish)
    monkeypatch.setattr(normalizer,'_apply_epistemic_admission',AsyncMock())
    stage=next(s for s in STAGES if s.name=='normalize_proposition')
    async def eligible(): return await db.fetch(stage.eligibility_sql,25)
    try:
        yield SimpleNamespace(db=db,con=con,claim=claim,sentence=sentence,section=section,node=node,
                              frame=frame,published=published,eligible=eligible)
    finally:
        process.stdin.close()
        await process.wait()


@pytest.mark.asyncio
async def test_blocked_revision_stops_repeated_discovery_and_duplicate_jobs(env,monkeypatch):
    e=env
    assert len(await e.eligible())==1
    assert await normalizer.normalize_claim_once(e.db,claim_id=e.claim) is None
    state=await e.db.fetchrow('SELECT * FROM aios.claim_normalization_state')
    assert state['outcome']=='blocked' and state['input_revision']==state['attempted_revision']
    assert state['integrity_status']=='incomplete' and state['reason_codes']
    validate=AsyncMock(side_effect=AssertionError('unchanged input must not revalidate'))
    monkeypatch.setattr(normalizer,'validate_claim',validate)
    for _ in range(75):
        assert await e.eligible()==[]
        assert await normalizer.normalize_claim_once(e.db,claim_id=e.claim) is None
    validate.assert_not_awaited()
    assert e.published==[]
    assert await e.db.fetchval('SELECT count(*) FROM aios.observation')==0


@pytest.mark.asyncio
@pytest.mark.parametrize('edit',[
    "UPDATE aios.claim_candidate SET raw_text='It rests here.'",
    "UPDATE aios.claim_semantic_frame SET resolved_subject='Renamon'",
    "UPDATE aios.claim_semantic_frame SET meta='{\"location\":\"here\"}'",
    "UPDATE aios.claim_context_resolution SET meta='{\"referent\":\"Renamon\"}'",
    "UPDATE aios.document_section SET content='It rests here.'",
    "UPDATE aios.extracted_sentence SET sentence_text='It rests here.'",
    "UPDATE aios.dag_node SET speaker_id='George'",
    "UPDATE aios.ingest_event SET source_kind='character_chat'",
    "UPDATE aios.claim_semantic_frame_projection SET primary_frame_id=NULL",
])
async def test_relevant_input_changes_reopen_blocked_claim(env,edit):
    e=env
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    assert await e.eligible()==[]
    await e.db.execute(edit)
    assert len(await e.eligible())==1
    state=await e.db.fetchrow('SELECT * FROM aios.claim_normalization_state')
    assert state['input_revision']>state['attempted_revision']


@pytest.mark.asyncio
async def test_timestamps_and_integrity_output_do_not_reopen_claim(env):
    e=env
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    await e.db.execute('UPDATE aios.claim_semantic_frame SET updated_at=now()')
    await e.db.execute('UPDATE aios.claim_context_resolution SET resolved_at=now()')
    await e.db.execute('UPDATE aios.document_section SET updated_at=now(),claims_extracted_at=now()')
    await e.db.execute('UPDATE aios.ingest_event SET processed_at=now()')
    await e.db.execute('UPDATE aios.claim_semantic_integrity SET checked_at=now()')
    assert await e.eligible()==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('version_field',['validator_version','normalizer_version','repair_version'])
async def test_new_policy_allows_one_repair_attempt(env,monkeypatch,version_field):
    e=env
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    await e.db.execute(f"UPDATE aios.claim_normalization_state SET {version_field}='old-policy'")
    assert len(await e.eligible())==1
    repair=AsyncMock(wraps=normalizer.repair_claim)
    monkeypatch.setattr(normalizer,'repair_claim',repair)
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    assert repair.await_count==1
    assert await e.eligible()==[]


@pytest.mark.asyncio
async def test_corrected_source_and_frame_can_publish_and_claim_delete_cascades(env):
    e=env
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    await e.db.execute("UPDATE aios.claim_candidate SET raw_text='Renamon rests.'")
    await e.db.execute("UPDATE aios.extracted_sentence SET sentence_text='Renamon rests.'")
    await e.db.execute("UPDATE aios.document_section SET content='Renamon rests.'")
    await e.db.execute("UPDATE aios.claim_semantic_frame SET subject_text='Renamon',resolved_subject='Renamon'")
    assert await normalizer.normalize_claim_once(e.db,claim_id=e.claim) is not None
    assert e.published==[e.claim]
    assert await e.eligible()==[]
    assert await e.db.fetchval('SELECT outcome FROM aios.claim_normalization_state')=='normalized'
    await e.db.execute('DELETE FROM aios.claim_candidate WHERE claim_id=$1',e.claim)
    assert await e.db.fetchval('SELECT count(*) FROM aios.claim_normalization_state')==0


@pytest.mark.asyncio
async def test_edit_during_validation_cannot_block_newer_revision(env,monkeypatch):
    e=env
    real_validate=normalizer.validate_claim
    async def validate(db,*,claim_id):
        receipt=await real_validate(db,claim_id=claim_id)
        await db.execute("UPDATE aios.claim_context_resolution SET meta='{\"changed\":true}'")
        return receipt
    monkeypatch.setattr(normalizer,'validate_claim',validate)
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    assert len(await e.eligible())==1
    assert await e.db.fetchval('SELECT outcome FROM aios.claim_normalization_state') is None


@pytest.mark.asyncio
async def test_new_claims_have_revision_tracking_from_insert(env):
    e=env
    another=uuid4()
    await e.db.execute("INSERT INTO aios.claim_candidate(claim_id,sentence_id,raw_text) VALUES($1,$2,'It rests.')",another,e.sentence)
    assert await e.db.fetchval('SELECT input_revision FROM aios.claim_normalization_state WHERE claim_id=$1',another)==1


@pytest.mark.asyncio
async def test_repair_budget_reopens_only_for_changed_input(env):
    e=env
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    await e.db.execute("UPDATE aios.claim_semantic_integrity SET repair_attempts=2")
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    assert await e.db.fetchval('SELECT repair_attempts FROM aios.claim_semantic_integrity')==2
    await e.db.execute("UPDATE aios.claim_context_resolution SET meta='{\"new_context\":true}'")
    await normalizer.normalize_claim_once(e.db,claim_id=e.claim)
    assert await e.db.fetchval('SELECT repair_attempts FROM aios.claim_semantic_integrity')==1
    assert await e.eligible()==[]
