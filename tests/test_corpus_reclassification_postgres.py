"""Repair classified recipe access with real SQL; preview cannot widen a profile."""
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from aios_app.corpus_catalog import CorpusCatalogService
from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
from test_contextual_corpus_postgres import env as search_env, search
from test_cognitive_episode_postgres import db

ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def env(search_env):
    e = search_env
    await e.db.pool.con.query('''
      DROP TABLE aios.character_corpus_lookup_trace;
      CREATE TABLE aios.character_goal_plan(status text);
      ALTER TABLE aios.corpus_scope ADD COLUMN display_name text;
      ALTER TABLE aios.corpus_document ADD COLUMN source_id text;
      ALTER TABLE aios.corpus_source_profile ADD COLUMN profile_key text;
      ALTER TABLE aios.corpus_source_profile ADD COLUMN source_id text;
      ALTER TABLE aios.corpus_source_profile ADD COLUMN domain_pattern text;
      ALTER TABLE aios.corpus_source_profile ADD COLUMN path_prefix text;
      ALTER TABLE aios.corpus_source_profile ADD COLUMN collection_key text;
      ALTER TABLE aios.corpus_source_profile ADD COLUMN epistemic_namespace text;
      ALTER TABLE aios.corpus_source_profile ADD COLUMN identity_binding text;
      ALTER TABLE aios.corpus_document_collection ADD COLUMN collection_key text;
      ALTER TABLE aios.corpus_document_collection ADD COLUMN assigned_by text;
      ALTER TABLE aios.corpus_document_collection ADD COLUMN meta jsonb DEFAULT '{}';
      ALTER TABLE aios.corpus_document_collection ADD UNIQUE(document_id,collection_key);
      ALTER TABLE aios.corpus_document_domain ADD COLUMN source text;
      ALTER TABLE aios.corpus_document_domain ADD COLUMN meta jsonb DEFAULT '{}';
      ALTER TABLE aios.corpus_document_domain ADD UNIQUE(document_id,knowledge_domain);
      ALTER TABLE aios.corpus_document_scope ADD UNIQUE(document_id,scope_key);
      ALTER TABLE aios.character_corpus_access ADD COLUMN updated_at timestamptz DEFAULT now();
      ALTER TABLE aios.character_knowledge_domain ADD COLUMN updated_at timestamptz DEFAULT now();
      CREATE TABLE aios.corpus_discovery_projection(indexed_at timestamptz);
    ''', exec=True)
    await e.db.pool.con.query((ROOT/'migrations/current/20261025_01_goal_dependency_continuation.sql').read_text(), exec=True)
    await e.db.execute("""INSERT INTO aios.corpus_source_profile(profile_id,profile_key,domain_pattern,
        path_prefix,collection_key,scope_key,knowledge_domain,epistemic_namespace,identity_binding,enabled)
        VALUES($1,'recipes','example.test','/recipes','recipes','canon.biology','biology','reference','external',true)""", uuid4())
    yield e


@pytest.mark.asyncio
async def test_preview_scoped_apply_search_and_current_deny(env):
    e = env
    rice, _ = await e.document('Rice practice', 'Rice recipe: measure water and simmer rice.', domain=None, scope='unclassified')
    eggs, _ = await e.document('Egg practice', 'Egg recipe: scramble eggs.', domain=None, scope='unclassified')
    other, _ = await e.document('Other page', 'Rice recipe unrelated page.', domain=None, scope='unclassified')
    await e.db.execute("UPDATE aios.corpus_document SET source_uri='https://example.test/recipes/rice' WHERE document_id=$1", rice)
    await e.db.execute("UPDATE aios.corpus_document SET source_uri='https://example.test/recipes/eggs' WHERE document_id=$1", eggs)
    await e.db.execute("UPDATE aios.corpus_document SET source_uri='https://example.test/recipes-evil/rice' WHERE document_id=$1", other)
    assert not (await search(e, 'rice')).hits
    catalog = CorpusCatalogService(e.db)
    epoch = await ProgressiveResearchService(e.db).corpus_change_epoch(e.instance)
    preview = await catalog.reclassify_profile('recipes', dry_run=True, document_ids=[rice, other])
    assert preview['matched_documents'] == 1 and preview['unmatched_selected'] == 1
    assert await ProgressiveResearchService(e.db).corpus_change_epoch(e.instance) == epoch
    assert not (await search(e, 'rice')).hits
    assert (await catalog.reclassify_profile('recipes', document_ids=[rice, other]))['reclassified_documents'] == 1
    assert await ProgressiveResearchService(e.db).corpus_change_epoch(e.instance) > epoch
    assert {h.document_id for h in (await search(e, 'rice')).hits} == {rice}
    assert await e.db.fetchval('SELECT count(*) FROM aios.corpus_document_domain WHERE document_id=ANY($1::uuid[])', [eggs, other]) == 0
    await e.db.execute("INSERT INTO aios.character_corpus_access(character_id,scope_key,allowed) VALUES('Arbitrary Fox','canon.biology',false)")
    assert not (await search(e, 'rice')).hits


@pytest.mark.asyncio
async def test_preview_full_profile_preserves_path_boundary(env):
    e = env
    rice, _ = await e.document('Rice', 'Rice recipe.', domain=None, scope='unclassified')
    await e.db.execute("UPDATE aios.corpus_document SET source_uri='https://example.test/recipes/rice' WHERE document_id=$1", rice)
    catalog = CorpusCatalogService(e.db)
    assert (await catalog.reclassify_profile('recipes', dry_run=True))['document_ids'] == [str(rice)]
    assert (await catalog.reclassify_profile('recipes', document_ids=[]))['reclassified_documents'] == 0
    assert not (await search(e, 'rice')).hits
