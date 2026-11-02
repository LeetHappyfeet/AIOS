"""Run receipt validation and the replay fast path against PostgreSQL via PGlite."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException

from aios_app.db import Database
from aios_app.ingest_api import ingest_message
from aios_app.ingest_sync import IngestSyncIn, validate_ingest_receipts
from aios_app.models import IngestIn

pytestmark = pytest.mark.skipif(not os.getenv('AIOS_PGLITE_MODULE'), reason='PGlite not configured')
ROOT = Path(__file__).resolve().parents[1]
DDL = '''
CREATE SCHEMA aios;
CREATE TYPE aios.event_kind AS ENUM ('chat_message');
CREATE TABLE aios.timeline(timeline_id uuid PRIMARY KEY,session_id uuid,character_id text,user_name text,scope_key text);
CREATE TABLE aios.character_instance(instance_id uuid PRIMARY KEY,character_id text);
CREATE TABLE aios.character_runtime_state(instance_id uuid PRIMARY KEY,timeline_id uuid,source_timeline_id uuid,source_head_node_id uuid,state_version bigint,updated_at timestamptz DEFAULT now());
CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY,session_id uuid,character_id text,user_name text,source text,source_event_id text,kind aios.event_kind DEFAULT 'chat_message',speaker_id text,speaker_role text,message_text text,payload jsonb,process_status text,process_error text,superseded_at timestamptz,dedupe_key text UNIQUE);
CREATE TABLE aios.dag_node(node_id uuid PRIMARY KEY,timeline_id uuid,event_id bigint,created_at timestamptz DEFAULT now());
CREATE TABLE aios.dag_edge(parent_node_id uuid,child_node_id uuid,timeline_id uuid);
'''


class Connection:
    def __init__(self, process):
        self.process = process
        self.queries = []

    async def query(self, sql, args=(), execute=False):
        self.queries.append(sql)
        def encode(v):
            if isinstance(v, UUID): return str(v)
            if isinstance(v, (list, tuple)): return [encode(x) for x in v]
            return v
        self.process.stdin.write((json.dumps(dict(sql=sql, args=encode(args), exec=execute)) + '\n').encode())
        await self.process.stdin.drain()
        response = json.loads(await self.process.stdout.readline())
        if 'error' in response: raise RuntimeError(response['error'])
        return response['value']

    async def fetch(self, sql, *args, **kwargs):
        result = await self.query(sql, args)
        for row in result['rows']:
            for field in result['fields']:
                if field['dataTypeID'] == 2950 and row[field['name']]:
                    row[field['name']] = UUID(row[field['name']])
        return result['rows']

    async def fetchrow(self, sql, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def execute(self, sql, *args):
        return await self.query(sql, args)

    @asynccontextmanager
    async def transaction(self):
        await self.query('BEGIN')
        try: yield
        except BaseException:
            await self.query('ROLLBACK')
            raise
        else: await self.query('COMMIT')


class Pool:
    def __init__(self, connection): self.con = connection
    @asynccontextmanager
    async def acquire(self): yield self.con


@pytest_asyncio.fixture
async def env():
    process = await asyncio.create_subprocess_exec('node', str(ROOT / 'tests/cognition_pglite_bridge.mjs'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    con = Connection(process)
    await con.query(DDL, execute=True)
    await con.query((ROOT / 'migrations/current/20261101_01_ingest_sync_replay.sql').read_text(), execute=True)
    db = Database('isolated')
    db.pool = Pool(con)
    session, instance, runtime, timeline, first, head, off_branch = [uuid4() for _ in range(7)]
    await db.execute("INSERT INTO aios.timeline VALUES($1,$3,'Alex','Mia','conversation'),($2,$3,'Alex','Mia','conversation')", runtime, timeline, session)
    await db.execute("INSERT INTO aios.character_instance VALUES($1,'Alex')", instance)
    await db.execute('INSERT INTO aios.character_runtime_state(instance_id,timeline_id,source_timeline_id,source_head_node_id,state_version) VALUES($1,$2,$3,$4,1)', instance, runtime, timeline, head)
    receipts = []
    for event, node in enumerate([first, head, off_branch], 1):
        text = f'Message {event}'
        payload = dict(source='SillyTavern', message_id=event-1, client_message_id=f'uuid-{event}', swipe_id=0,
            text=text, character_id='Alex', user_name='Mia', speaker_type='character', speaker_id='Alex',
            recipient_id='Mia', viewpoint_id='Alex', pivot_character_id='Alex', identity_ruleset='character-id-v1', scope_key='conversation')
        key = f'{session}::sillytavern::character:{event-1}::{hashlib.sha256(text.encode()).hexdigest()}'
        await db.execute("INSERT INTO aios.ingest_event(event_id,session_id,character_id,user_name,source,source_event_id,speaker_id,speaker_role,message_text,payload,process_status,dedupe_key) VALUES($1,$2,'Alex','Mia','SillyTavern',$3,'Alex','character',$4,$5::jsonb,'new',$6)", event, session, f'character:{event-1}', text, json.dumps(payload), key)
        await db.execute('INSERT INTO aios.dag_node(node_id,timeline_id,event_id) VALUES($1,$2,$3)', node, timeline, event)
        receipts.append(dict(message_id=event-1, event_id=event, node_id=node, speaker_type='character',
            speaker_id='Alex', text_hash=hashlib.sha256(text.encode()).hexdigest(), message_uuid=f'uuid-{event}', swipe_id=0))
    await db.execute('INSERT INTO aios.dag_edge VALUES($1,$2,$3)', first, head, timeline)
    req = IngestSyncIn(session_id=session, character_id='Alex', user_name='Mia', source_head_node_id=head, receipts=receipts[:2])
    try: yield db, con, instance, req, receipts
    finally:
        process.stdin.close()
        await process.wait()


@pytest.mark.asyncio
async def test_receipts_are_validated_without_persistent_writes(env):
    db, con, instance, req, _ = env
    before = await db.fetchrow('SELECT xmin::text AS version FROM aios.ingest_event WHERE event_id=1')
    con.queries.clear()
    result = await validate_ingest_receipts(db, instance, req)
    assert not result['replay_required'] and result['head_matches']
    assert all(r['valid'] for r in result['receipts'])
    assert len([q for q in con.queries if 'WITH RECURSIVE' in q]) == 1
    assert not any(q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in con.queries)
    assert before == await db.fetchrow('SELECT xmin::text AS version FROM aios.ingest_event WHERE event_id=1')


@pytest.mark.asyncio
async def test_missing_receipt_and_off_branch_nodes_cannot_be_skipped(env):
    db, _, instance, req, receipts = env
    req.receipts.append(type(req.receipts[0])(**receipts[2]))
    req.receipts[0].event_id = 999
    result = await validate_ingest_receipts(db, instance, req)
    assert [r['valid'] for r in result['receipts']] == [False, True, False]
    assert result['replay_required']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['superseded', 'error', 'pending', 'hash', 'uuid', 'swipe', 'speaker', 'node'])
async def test_stale_or_mismatched_receipts_require_replay(env, mutation):
    db, _, instance, req, _ = env
    if mutation in {'superseded', 'error', 'pending'}:
        update = {'superseded': 'superseded_at=now()', 'error': "process_status='error'", 'pending': "process_error='participant_binding_pending'"}[mutation]
        await db.execute('UPDATE aios.ingest_event SET '+update+' WHERE event_id=1')
    else:
        field, value = {'hash': ('text_hash', '0'*64), 'uuid': ('message_uuid', 'wrong'),
                        'swipe': ('swipe_id', 2), 'speaker': ('speaker_id', 'Other'), 'node': ('node_id', uuid4())}[mutation]
        setattr(req.receipts[0], field, value)
    result = await validate_ingest_receipts(db, instance, req)
    assert not result['receipts'][0]['valid'] and result['replay_required']


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('session_id', uuid4()), ('character_id', 'Other'), ('user_name', 'Other'), ('scope_key', 'Other')])
async def test_foreign_mapping_is_rejected(env, field, value):
    db, _, instance, req, _ = env
    setattr(req, field, value)
    with pytest.raises(HTTPException) as exc:
        await validate_ingest_receipts(db, instance, req)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_empty_page_and_changed_head_are_explicit(env):
    db, _, instance, req, _ = env
    req.receipts = []
    req.source_head_node_id = uuid4()
    result = await validate_ingest_receipts(db, instance, req)
    assert result['checked_count'] == 0 and result['replay_required']
    assert not result['complete_transcript_verified']


@pytest.mark.asyncio
async def test_real_legacy_replay_uses_reads_and_preserves_event_row(env):
    db, con, instance, req, _ = env
    row = await db.fetchrow('SELECT xmin::text AS version FROM aios.ingest_event WHERE event_id=1')
    con.queries.clear()
    out = await ingest_message(db, IngestIn(session_id=req.session_id, character_id='Alex', user_name='Mia',
        speaker_id='Alex', speaker_type='character', recipient_id='Mia', text='Message 1',
        payload=dict(source='SillyTavern', message_id=0, client_message_id='uuid-1', swipe_id=0)))
    assert out.event_id == 1 and out.source_head_node_id == req.source_head_node_id
    assert not any(q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in con.queries)
    assert row == await db.fetchrow('SELECT xmin::text AS version FROM aios.ingest_event WHERE event_id=1')


@pytest.mark.asyncio
async def test_cycle_is_finite_and_old_receipts_outside_branch_window_are_unvalidated(env):
    db, _, instance, req, _ = env
    first, head = [r.node_id for r in req.receipts]
    timeline = (await db.fetchrow('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', instance))['source_timeline_id']
    await db.execute('INSERT INTO aios.dag_edge VALUES($1,$2,$3)', head, first, timeline)
    assert not (await validate_ingest_receipts(db, instance, req))['replay_required']
    await db.execute('CREATE INDEX ON aios.dag_edge(timeline_id,child_node_id)')
    await db.execute("INSERT INTO aios.dag_node(node_id,timeline_id,event_id) SELECT md5('long-'||n::text)::uuid,$1,1000+n FROM generate_series(1,4100) n", timeline)
    await db.execute("INSERT INTO aios.dag_edge SELECT CASE WHEN n=1 THEN $1 ELSE md5('long-'||(n-1)::text)::uuid END,md5('long-'||n::text)::uuid,$2 FROM generate_series(1,4100) n", head, timeline)
    new_head = UUID(hashlib.md5(b'long-4100').hexdigest())
    await db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1', instance, new_head)
    req.source_head_node_id = new_head
    result = await validate_ingest_receipts(db, instance, req)
    assert result['branch_window_exhausted']
    assert result['replay_required'] and not any(r['valid'] for r in result['receipts'])
