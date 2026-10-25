"""Exercise production corpus SQL and cognition-to-render references in isolated PostgreSQL."""
import asyncio
import os
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from aios_app.epistemic.cognitive_context import (
    CognitiveAttentionInputs, CognitiveContextService, PreparedRetrievalSnapshot,
    contextual_corpus_query,
)
from aios_app.epistemic.research import CorpusSearchService, corpus_passage
from aios_app.hud.frame import _budget_corpus_references
from aios_app.hud.render_text import render_hud_text
from test_cognitive_episode_postgres import db

pytestmark = pytest.mark.skipif(not os.getenv('AIOS_PGLITE_MODULE'), reason='isolated PostgreSQL not configured')


@pytest_asyncio.fixture
async def env(db):
    await db.pool.con.query('''
      ALTER TABLE aios.character_instance ADD COLUMN character_id text;
      CREATE TABLE aios.character_knowledge_domain(character_id text,knowledge_domain text,enabled boolean);
      CREATE TABLE aios.character_corpus_access(character_id text,scope_key text,allowed boolean);
      CREATE TABLE aios.corpus_scope(scope_key text PRIMARY KEY,access_class text);
      CREATE TABLE aios.corpus_document(document_id uuid PRIMARY KEY,title text,source_uri text,
          epistemic_namespace text DEFAULT 'canon',identity_binding text DEFAULT 'external');
      CREATE TABLE aios.corpus_section(section_id uuid PRIMARY KEY,document_id uuid,heading text,
          content text,search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english',content)) STORED);
      CREATE TABLE aios.corpus_document_scope(document_id uuid,scope_key text);
      CREATE TABLE aios.corpus_document_domain(document_id uuid,knowledge_domain text);
      CREATE TABLE aios.corpus_document_facet(document_id uuid,facet_value text);
      CREATE TABLE aios.corpus_source_profile(profile_id uuid,scope_key text,knowledge_domain text,enabled boolean);
      CREATE TABLE aios.corpus_document_collection(document_id uuid,profile_id uuid);
      CREATE TABLE aios.character_corpus_lookup_trace(instance_id uuid,source_node_key text,
          query_hash text,diagnostics jsonb,updated_at timestamptz DEFAULT now(),
          PRIMARY KEY(instance_id,source_node_key,query_hash));
      CREATE TABLE aios.character_research_event(research_id uuid DEFAULT gen_random_uuid(),
          instance_id uuid,character_id text,query_text text,query_terms text[],status text,result_count int);
      CREATE TABLE aios.character_corpus_exposure(research_id uuid,section_id uuid,rank int,score float,meta jsonb,
          UNIQUE(research_id,section_id));
      CREATE TABLE aios.knowledge_topic(topic_id uuid,visibility text,status text,normalized_label text);
      CREATE TABLE aios.knowledge_topic_alias(topic_id uuid,normalized_alias text);
      CREATE TABLE aios.knowledge_topic_relation(source_topic_id uuid,target_topic_id uuid,status text);
      CREATE TABLE aios.knowledge_topic_source(topic_id uuid,section_id uuid,document_id uuid,status text);
    ''', exec=True)
    instance=uuid4()
    await db.execute('INSERT INTO aios.character_instance VALUES($1,$2)',instance,'Arbitrary Fox')
    await db.execute("INSERT INTO aios.character_knowledge_domain VALUES('Arbitrary Fox','digimon',true),('Arbitrary Fox','biology',true)")
    await db.execute("INSERT INTO aios.corpus_scope VALUES('canon.digimon','domain'),('canon.biology','domain'),('public.other','public'),('canon.fanwork','public')")
    async def document(title, content, *, domain='digimon', scope='canon.digimon'):
        doc,section=uuid4(),uuid4()
        await db.execute("INSERT INTO aios.corpus_document(document_id,title,source_uri) VALUES($1,$2,$3)",doc,title,'https://example.test/'+str(doc))
        await db.execute("INSERT INTO aios.corpus_section(section_id,document_id,heading,content) VALUES($1,$2,'Details',$3)",section,doc,content)
        await db.execute('INSERT INTO aios.corpus_document_scope VALUES($1,$2)',doc,scope)
        if domain: await db.execute('INSERT INTO aios.corpus_document_domain VALUES($1,$2)',doc,domain)
        return doc,section
    primary=await document('Digimon guide','Unrelated introductory material. '*110+'Digimon evolution changes a creature into a new form.')
    second=await document('Biology guide','Digimon evolution can follow multiple branches.',domain='biology',scope='canon.biology')
    await document('Unassigned public guide','Digimon evolution secret unassigned material.',domain=None,scope='public.other')
    await document('Fan fiction','Digimon evolution secret fanwork material.',scope='canon.fanwork')
    searcher=CorpusSearchService(db)
    searcher.semantic=SimpleNamespace(search_corpus_discovery=lambda *a,**kw: [])
    yield SimpleNamespace(db=db,instance=instance,searcher=searcher,primary=primary,second=second,document=document)


def attention(text, *, route='general'):
    return CognitiveAttentionInputs(
        recent_newest=[{'speaker_id':'Mia','speaker_role':'user','message_text':text}],
        visible_source_node_ids=frozenset(),focus_text=text,plugin_focus_text='',retrieval_focus_text=text,
        goals=[],knowledge_route=route)


async def search(e, query='digimon OR evolution', **kw):
    return await e.searcher.search(instance_id=e.instance,query=query,assigned_only=True,
        lexical_only=True,include_fanwork=False,limit=3,**kw)


@pytest.mark.asyncio
async def test_assigned_multiple_corpora_late_passage_and_acl_revocation(env):
    e=env
    result=await search(e)
    assert {hit.document_id for hit in result.hits}=={e.primary[0],e.second[0]}
    hit=next(h for h in result.hits if h.document_id==e.primary[0])
    assert hit.passage_start>1800 and 'Digimon evolution' in hit.excerpt
    assert hit.source_uri.startswith('https://') and hit.identity_binding=='external'
    assert result.candidate_count==2
    await e.db.execute("INSERT INTO aios.character_corpus_access VALUES('Arbitrary Fox','canon.digimon',false)")
    assert {h.document_id for h in (await search(e)).hits}=={e.second[0]}
    await e.db.execute("UPDATE aios.character_knowledge_domain SET enabled=false")
    assert (await search(e)).status=='no_assignment'


@pytest.mark.asyncio
async def test_unrelated_conversation_has_no_reference_even_with_assigned_corpora(env):
    assert not (await search(env,'cooking OR rice OR pot')).hits
    # A name alone cannot inject arbitrary canon.
    assert contextual_corpus_query(attention('Arbitrary Fox'),character_id='Arbitrary Fox') is None


@pytest.mark.asyncio
async def test_contextual_reference_reaches_render_without_acquisition_or_completion(env,monkeypatch):
    from aios_app.entity_memory.retrieval import RDFRecallService, RecallResult
    from aios_app.epistemic import cognitive_context as module
    e=env
    service=CognitiveContextService(e.db)
    service.research.searcher=e.searcher
    monkeypatch.setattr(module,'current_message_cognition',AsyncMock(return_value=[]))
    service._prepared_or_resolve=AsyncMock(return_value=(PreparedRetrievalSnapshot([],[],{},time.monotonic()),False))
    monkeypatch.setattr(RDFRecallService,'cached',AsyncMock(return_value=RecallResult()))
    service.corpus_learning.evaluate_and_acquire=AsyncMock(side_effect=AssertionError('lookup is not study'))
    service.corpus_reinforcement.reinforce_from_focus=AsyncMock(side_effect=AssertionError('context is not reinforcement'))
    ctx=SimpleNamespace(instance_id=e.instance,character_id='Arbitrary Fox',source_head_node_id=uuid4(),state_version=3)
    focus=attention('Digimon evolution follows branching paths.')
    snapshot, duplicate = await asyncio.gather(
        service.resolve_knowledge(ctx,SimpleNamespace(),focus),
        service.resolve_knowledge(ctx,SimpleNamespace(),focus))
    assert snapshot.corpus_references == duplicate.corpus_references
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_research_event') == 1
    references=_budget_corpus_references(snapshot.corpus_references,260,focus.focus_text)
    text=render_hud_text({'identity':{'character_id':ctx.character_id},'presence':{},'state':{},'scene':{},'corpus_references':references})
    assert 'CORPUS REFERENCES' in text and 'Digimon evolution' in text
    assert 'binding=external' in text and 'namespace=canon' in text
    assert 'secret unassigned' not in text and 'secret fanwork' not in text
    assert snapshot.knowledge==snapshot.goals==snapshot.beliefs==snapshot.recalled_memories==[]
    assert snapshot.corpus_demand['lookup_status']=='references_found'
    service.corpus_learning.evaluate_and_acquire.assert_not_awaited()
    service.corpus_reinforcement.reinforce_from_focus.assert_not_awaited()
    personal=attention('Have you ever cooked rice before?',route='character')
    personal_snapshot=await service.resolve_knowledge(ctx,SimpleNamespace(),personal)
    assert personal_snapshot.corpus_references==[]
    async def unavailable(**kw):
        await asyncio.sleep(5)
    service.research.searcher.search=unavailable
    service.retrieval_policy=replace(service.retrieval_policy,corpus_timeout_seconds=.01)
    failed=await service.resolve_knowledge(ctx,SimpleNamespace(),focus)
    assert failed.corpus_references==[] and failed.corpus_demand['lookup_status']=='lookup_unavailable'
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_corpus_lookup_trace WHERE diagnostics->>'lookup_status'='lookup_unavailable'") == 1


@pytest.mark.asyncio
async def test_semantic_fallback_remains_assigned_and_denied_sources_stay_hidden(env):
    e=env
    e.searcher.semantic=SimpleNamespace(search_corpus_discovery=lambda *a,**kw:[
        ('corpus',.9,{'section_id':str(e.primary[1])})])
    result=await e.searcher.search(instance_id=e.instance,query='transformation',assigned_only=True,include_fanwork=False)
    assert len(result.hits)==1 and 'semantic' in result.hits[0].retrieval_methods
    await e.db.execute("INSERT INTO aios.character_corpus_access VALUES('Arbitrary Fox','canon.digimon',false)")
    assert not (await e.searcher.search(instance_id=e.instance,query='transformation',assigned_only=True,include_fanwork=False)).hits


@pytest.mark.asyncio
async def test_slow_corpus_maintenance_rolls_back_and_preserves_hud_references(env, monkeypatch, caplog):
    from aios_app.entity_memory.retrieval import RDFRecallService, RecallResult
    from aios_app.epistemic import cognitive_context as module
    e = env
    service = CognitiveContextService(e.db)
    service.research.searcher = e.searcher
    service.retrieval_policy = replace(service.retrieval_policy, corpus_maintenance_timeout_seconds=.03)
    monkeypatch.setattr(module, 'current_message_cognition', AsyncMock(return_value=[]))
    service._prepared_or_resolve = AsyncMock(return_value=(PreparedRetrievalSnapshot([], [], {}, time.monotonic()), False))
    monkeypatch.setattr(RDFRecallService, 'cached', AsyncMock(return_value=RecallResult()))
    ctx = SimpleNamespace(instance_id=e.instance, character_id='Arbitrary Fox', source_head_node_id=uuid4(), state_version=3)
    focus = attention('What is digimon evolution?')
    cancelled = asyncio.Event()

    async def blocked_maintenance(**kw):
        await e.db.execute("INSERT INTO aios.character_corpus_access VALUES('Arbitrary Fox','maintenance_marker',true)")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.corpus_reinforcement.reinforce_from_focus = AsyncMock(side_effect=blocked_maintenance)
    service.semantic_corpus_reinforcement.reinforce_from_knowledge = AsyncMock()
    snapshot = await asyncio.wait_for(service.resolve_knowledge(ctx, SimpleNamespace(), focus), 2)
    assert cancelled.is_set()
    assert snapshot.corpus_references
    assert snapshot.corpus_demand['maintenance_status'] == 'deferred'
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_corpus_access WHERE scope_key='maintenance_marker'") == 0
    service.semantic_corpus_reinforcement.reinforce_from_knowledge.assert_not_awaited()
    assert 'CORPUS REFERENCES' in render_hud_text({'corpus_references': snapshot.corpus_references})
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_timed_out_lookup_skips_maintenance_and_returns_cognition(env, monkeypatch, caplog):
    from aios_app.entity_memory.retrieval import RDFRecallService, RecallResult
    from aios_app.epistemic import cognitive_context as module
    e = env
    service = CognitiveContextService(e.db)
    service.retrieval_policy = replace(service.retrieval_policy, corpus_timeout_seconds=.01)
    monkeypatch.setattr(module, 'current_message_cognition', AsyncMock(return_value=[]))
    service._prepared_or_resolve = AsyncMock(return_value=(PreparedRetrievalSnapshot([], [], {}, time.monotonic()), False))
    monkeypatch.setattr(RDFRecallService, 'cached', AsyncMock(return_value=RecallResult()))
    async def blocked_search(**kw):
        await asyncio.Event().wait()
    service.research.search = AsyncMock(side_effect=blocked_search)
    service.corpus_reinforcement.reinforce_from_focus = AsyncMock()
    service.semantic_corpus_reinforcement.reinforce_from_knowledge = AsyncMock()
    ctx = SimpleNamespace(instance_id=e.instance, character_id='Arbitrary Fox', source_head_node_id=uuid4(), state_version=3)
    for text in ('Digimon evolution follows branching paths.', 'What is digimon evolution?'):
        snapshot = await asyncio.wait_for(service.resolve_knowledge(ctx, SimpleNamespace(), attention(text)), 1)
        assert snapshot.corpus_demand['lookup_status'] == 'lookup_unavailable'
        assert snapshot.corpus_references == []
        assert snapshot.current_events[0]['message_text'] == text
    service.corpus_reinforcement.reinforce_from_focus.assert_not_awaited()
    service.semantic_corpus_reinforcement.reinforce_from_knowledge.assert_not_awaited()
    assert len(caplog.records) == 2
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_many_denied_lexical_candidates_cannot_hide_authorized_references(env):
    e = env
    await e.db.execute("INSERT INTO aios.character_corpus_access VALUES('Arbitrary Fox','canon.blocked',false)")
    await e.db.execute("INSERT INTO aios.corpus_scope VALUES('canon.blocked','domain')")
    await e.db.execute("""INSERT INTO aios.corpus_document(document_id,title)
        SELECT gen_random_uuid(),'Denied '||i FROM generate_series(1,80) i""")
    await e.db.execute("""INSERT INTO aios.corpus_section(section_id,document_id,heading,content)
        SELECT gen_random_uuid(),document_id,'Blocked',repeat('Digimon evolution ',20)
        FROM aios.corpus_document WHERE title LIKE 'Denied %'""")
    await e.db.execute("""INSERT INTO aios.corpus_document_scope
        SELECT document_id,'canon.blocked' FROM aios.corpus_document WHERE title LIKE 'Denied %'""")
    await e.db.execute("""INSERT INTO aios.corpus_document_domain
        SELECT document_id,'digimon' FROM aios.corpus_document WHERE title LIKE 'Denied %'""")
    result = await search(e)
    assert {hit.document_id for hit in result.hits} == {e.primary[0], e.second[0]}
    assert result.candidate_count == 2
