"""SQL access boundaries plus executable SPARQL over real projected graphs."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from rdflib import Dataset, URIRef

from aios_app.entity_memory.retrieval import RDFRecallService, scope_key, terms_for
from aios_app.entity_memory.service import collect_entity_memory_once, project_entity_memory_once
from test_entity_memory_postgres import env as base_db, seed

ROOT=Path(__file__).resolve().parents[1]


class Graphs:
    def __init__(self): self.data={"char":Dataset(),"world":Dataset()}; self.reads=0
    def update(self,dataset,query): self.data[dataset].update(query)
    def query(self,dataset,query):
        self.reads+=1
        return json.loads(self.data[dataset].query(query).serialize(format="json"))


@pytest_asyncio.fixture
async def env(base_db):
    db=base_db
    # Same production SQL, with only the existing upstream admission function
    # represented by an explicit eligibility flag in this isolated fixture.
    await db.pool.con.query('''
      CREATE TABLE aios.character_instance(instance_id uuid PRIMARY KEY);
      ALTER TABLE aios.dag_node ADD COLUMN speaker_role text DEFAULT 'character';
      CREATE TABLE aios.dag_edge(timeline_id uuid,parent_node_id uuid,child_node_id uuid);
      ALTER TABLE aios.claim_candidate ADD COLUMN raw_text text;
      ALTER TABLE aios.claim_context_resolution ADD COLUMN claim_kind text DEFAULT 'BELIEF';
      CREATE TABLE aios.proposition(proposition_id uuid PRIMARY KEY,atom_id uuid,canonical_text text,
          subject_norm text,predicate_norm text,object_norm text,polarity smallint,modality text);
      CREATE TABLE aios.knowledge_acquisition_event(acquisition_id uuid PRIMARY KEY,instance_id uuid,
          claim_id uuid,proposition_id uuid,dag_node_id uuid,confidence float,epistemic_status text,
          created_at timestamptz DEFAULT now(),eligible boolean DEFAULT true);
      CREATE TABLE aios.epistemic_authority_admission(acquisition_id uuid PRIMARY KEY,
          authority_state text,origin_kind text,epistemic_mode text,authorized_uses text[]);
      CREATE TABLE aios.semantic_evidence_admission(acquisition_id uuid PRIMARY KEY,status text);
      CREATE FUNCTION aios.semantic_acquisition_source_eligible(id uuid) RETURNS boolean LANGUAGE SQL AS $$
          SELECT COALESCE((SELECT eligible FROM aios.knowledge_acquisition_event WHERE acquisition_id=id),false)
      $$;
    ''',exec=True)
    await db.pool.con.query((ROOT/'migrations/current/20261023_01_rdf_assisted_recall.sql').read_text(),exec=True)
    instance=uuid4()
    await db.execute("INSERT INTO aios.character_instance VALUES($1)",instance)
    section,claim=await seed(db)
    row=await db.fetchrow("SELECT n.node_id,n.timeline_id,t.world_id FROM aios.document_section ds JOIN aios.dag_node n ON n.node_id=ds.node_id JOIN aios.timeline t ON t.timeline_id=n.timeline_id WHERE ds.section_id=$1",section)
    ctx=SimpleNamespace(instance_id=instance,character_id="Renamon",source_head_node_id=row['node_id'],
         source_timeline_id=row['timeline_id'],world_id=row['world_id'],
         cognitive_instance_ids=(instance,),lineage_instance_ids=(instance,),state_version=1)
    await collect_entity_memory_once(db)
    graphs=Graphs()
    await project_entity_memory_once(db,graphs)
    yield SimpleNamespace(db=db,ctx=ctx,section=section,claim=claim,graphs=graphs,
                          service=RDFRecallService(db,graphs))


@pytest.mark.asyncio
async def test_rdf_hit_reaches_validated_source_and_read_only_cache(env):
    e=env
    result=await e.service.search(e.ctx,"Where is the blue mug?")
    assert result.status=='partial', result.diagnostics
    assert len(result.hits)==1
    hit=result.hits[0]
    assert 'blue mug' in hit['text']
    assert hit['source']=='source_dag' and not hit['durable_knowledge']
    assert hit['provenance']['offset_basis']=='section'
    assert hit['provenance']['current_world_state'] is False
    before=e.graphs.reads
    async def no_write(*a): raise AssertionError('HUD must not write')
    e.db.execute=no_write
    recalled=await e.service.cached(e.ctx,"Where is the blue mug?")
    assert recalled.hits==result.hits
    assert e.graphs.reads==before


@pytest.mark.asyncio
async def test_new_head_requires_real_dag_ancestry_and_siblings_are_excluded(env):
    e=env
    head=uuid4()
    await e.db.execute("INSERT INTO aios.ingest_event VALUES(2,NULL)")
    await e.db.execute("INSERT INTO aios.dag_node(node_id,timeline_id,event_id,kind) VALUES($1,$2,2,'chat_message')",head,e.ctx.source_timeline_id)
    e.ctx.source_head_node_id=head
    result=await e.service.search(e.ctx,"blue mug")
    assert not result.hits
    await e.db.execute("INSERT INTO aios.dag_edge VALUES($1,$2,$3)",e.ctx.source_timeline_id,
                       (await e.db.fetchrow('SELECT node_id FROM aios.document_section WHERE section_id=$1',e.section))['node_id'],head)
    result=await e.service.search(e.ctx,"blue mug")
    assert result.hits
    # Adding an unrelated sibling does not make it an ancestor.
    await e.db.execute("DELETE FROM aios.dag_edge")
    assert not (await e.service.validate_refs(e.ctx,result.refs)).hits


@pytest.mark.asyncio
async def test_other_chat_same_character_cannot_read_sources_without_acquisition(env):
    e=env
    other=SimpleNamespace(**vars(e.ctx)); other.instance_id=uuid4()
    other.cognitive_instance_ids=(other.instance_id,); other.lineage_instance_ids=(other.instance_id,)
    other.source_timeline_id=uuid4(); other.source_head_node_id=uuid4()
    await e.db.execute("INSERT INTO aios.character_instance VALUES($1)",other.instance_id)
    assert not (await e.service.search(other,"blue mug")).hits
    assert scope_key(other)!=scope_key(e.ctx)


@pytest.mark.asyncio
async def test_stale_cache_source_edits_and_supersession_are_rejected(env):
    e=env
    result=await e.service.search(e.ctx,"blue mug")
    await e.db.execute("UPDATE aios.document_section SET content='changed source' WHERE section_id=$1",e.section)
    assert not (await e.service.validate_refs(e.ctx,result.refs)).hits
    assert not (await e.service.cached(e.ctx,"blue mug")).hits
    await e.db.execute("UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=1")
    assert not (await e.service.validate_refs(e.ctx,result.refs)).hits


@pytest.mark.asyncio
async def test_forged_rdf_coordinates_or_payload_hash_are_never_evidence(env):
    e=env
    result=await e.service.search(e.ctx,"blue mug")
    assert result.refs
    for change in ({'text':'a forged cookbook'},{'start':999},{'payload_hash':'old'},{'section_id':str(uuid4())}):
        assert not (await e.service.validate_refs(e.ctx,[{**result.refs[0],**change}])).hits


async def atom(e):
    pid,aid,acq=uuid4(),uuid4(),uuid4()
    await e.db.execute("INSERT INTO aios.proposition VALUES($1,$2,'Renamon does not like the couch','Renamon','like','couch',-1,'asserted')",pid,aid)
    await e.db.execute("INSERT INTO aios.knowledge_acquisition_event(acquisition_id,instance_id,claim_id,proposition_id,confidence,epistemic_status) VALUES($1,$2,$3,$4,0.9,'accepted')",acq,e.ctx.instance_id,e.claim,pid)
    await e.db.execute("INSERT INTO aios.epistemic_authority_admission VALUES($1,'attested','source','reported',ARRAY['reflection'])",acq)
    await e.db.execute("INSERT INTO aios.semantic_evidence_admission VALUES($1,'active')",acq)
    e.graphs.update('char',f'''PREFIX char:<urn:aios:char#> INSERT DATA {{ GRAPH <urn:aios:char:Renamon:epistemic> {{
       <urn:test:belief> char:characterInstance <urn:aios:character-instance:{e.ctx.instance_id}>;
           char:semanticAtom <urn:aios:semantic-atom:{aid}> . }} }}''')
    return pid,aid,acq


@pytest.mark.asyncio
async def test_legacy_atom_reader_keeps_polarity_and_rechecks_authority(env):
    e=env
    pid,aid,acq=await atom(e)
    result=await e.service.search(e.ctx,"couch")
    assert len(result.hits)==1, result.diagnostics
    hit=result.hits[0]
    assert hit['source']=='char' and hit['durable_knowledge']
    assert hit['provenance']['polarity']==-1
    await e.db.execute("UPDATE aios.epistemic_authority_admission SET authorized_uses='{}' WHERE acquisition_id=$1",acq)
    assert not (await e.service.validate_refs(e.ctx,result.refs)).hits


@pytest.mark.asyncio
async def test_fuseki_failure_returns_unavailable_not_ignorance(env):
    e=env
    def fail(*args): raise OSError('offline')
    e.graphs.query=fail
    result=await e.service.search(e.ctx,"blue mug")
    assert result.status=='unavailable'
    assert not result.hits
    assert await e.db.fetchval("SELECT status FROM aios.entity_recall_trace WHERE trace_id=$1",__import__('uuid').UUID(result.trace_id))=='unavailable'


@pytest.mark.asyncio
async def test_goal_point_lookup_consumes_validated_source_refs_without_search(env,monkeypatch):
    from aios_app.agent.goal_planning import GoalPlanningService,build_plan_prompt
    e=env
    result=await e.service.search(e.ctx,"blue mug")
    async def forbidden(*args,**kwargs): raise AssertionError('planner must not search')
    monkeypatch.setattr(RDFRecallService,'search',forbidden)
    rows,status=await GoalPlanningService(e.db).linked_knowledge(e.ctx,[{'rdf_refs':result.refs}])
    assert status=='linked_source_evidence'
    prompt,ids=build_plan_prompt('find mug',[],rows,[],{},[])
    assert 'blue mug' in prompt
    assert 'source_report_only' in prompt
    assert result.hits[0]['evidence_id'] in ids
    await e.db.execute("UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=1")
    rows,_=await GoalPlanningService(e.db).linked_knowledge(e.ctx,[{'rdf_refs':result.refs}])
    assert rows==[]


@pytest.mark.asyncio
async def test_reference_links_survive_requirement_refresh_and_feed_the_next_plan(env):
    from aios_app.agent.goal_dependencies import GoalKnowledgeDependencyService
    from aios_app.agent.goal_planning import GoalPlanningService,build_plan_prompt
    e=env
    await e.db.pool.con.query('''
      CREATE TABLE aios.character_agent_goal(goal_id uuid PRIMARY KEY,instance_id uuid,status text);
      CREATE TABLE aios.character_goal_knowledge_requirement(requirement_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        instance_id uuid,goal_id uuid,requirement_key text,question text,query_text text,coverage_status text,
        coverage_score float,coverage_source text,retrieval_state text,evidence jsonb,source_node_id uuid,
        created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now(),UNIQUE(instance_id,goal_id,requirement_key));
      CREATE TABLE aios.character_goal_research_requirement_link(requirement_id uuid,instance_id uuid,dossier_id uuid,linked_at timestamptz);
    ''',exec=True)
    goal=uuid4()
    await e.db.execute("INSERT INTO aios.character_agent_goal VALUES($1,$2,'active')",goal,e.ctx.instance_id)
    deps=GoalKnowledgeDependencyService(e.db)
    req=await deps.reconcile(instance_id=e.ctx.instance_id,goal_id=goal,question='Where is the mug?',
        query_text='blue mug',demand={'coverage_status':'missing'})
    result=await e.service.search(e.ctx,'blue mug')
    hits=[{**h,'provenance':{**h['provenance'],'rdf_ref':ref}} for h,ref in zip(result.hits,result.refs)]
    await deps.record_inquiry(instance_id=e.ctx.instance_id,goal_id=goal,requirement_id=req['requirement_id'],
        revision='epoch',receipt={'inquiry_id':'lookup','status':'partial','result':{'hits':hits}})
    # Ordinary HUD/coverage refresh cannot erase the linked source references.
    await deps.reconcile(instance_id=e.ctx.instance_id,goal_id=goal,question='Where is the mug?',
        query_text='blue mug',demand={'coverage_status':'partial'})
    linked=(await deps.for_goals(instance_id=e.ctx.instance_id,goal_ids=[goal]))[goal]
    assert linked[0]['rdf_refs']
    rows,status=await GoalPlanningService(e.db).linked_knowledge(e.ctx,linked)
    prompt,_=build_plan_prompt('find mug',[],rows,linked,{},[])
    assert 'blue mug' in prompt and 'source_report_only' in prompt
    assert await e.db.fetchval('SELECT status FROM aios.character_agent_goal WHERE goal_id=$1',goal)=='active'


@pytest.mark.asyncio
async def test_discovery_revision_changes_with_relevant_evidence_not_just_a_new_head(env):
    e=env
    first=await e.service.discovery_revision(e.ctx,'blue mug')
    head=uuid4()
    await e.db.execute("INSERT INTO aios.ingest_event VALUES(2,NULL)")
    await e.db.execute("INSERT INTO aios.dag_node(node_id,timeline_id,event_id,kind) VALUES($1,$2,2,'chat_message')",head,e.ctx.source_timeline_id)
    await e.db.execute("INSERT INTO aios.dag_edge VALUES($1,$2,$3)",e.ctx.source_timeline_id,e.ctx.source_head_node_id,head)
    e.ctx.source_head_node_id=head
    assert await e.service.discovery_revision(e.ctx,'blue mug')==first
    await e.db.execute("UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=1")
    assert await e.service.discovery_revision(e.ctx,'blue mug')!=first


@pytest.mark.asyncio
async def test_read_only_query_plan_diagnostic_uses_production_sql(env):
    from aios_app.entity_memory.diagnostics import query_plans
    from test_cognitive_episode_postgres import PGConnection, PGPool
    e = env
    # The diagnostic uses nested savepoints; use the savepoint-capable bridge
    # on the same isolated database for this test.
    e.db.pool = PGPool(PGConnection(e.db.pool.con.process))
    for analyze in (False, True):
        report = await query_plans(e.db, e.ctx, 'blue mug', analyze=analyze)
        assert not report.get('timed_out')
        assert report['analyze'] == analyze
        assert set(report['plans']) == {'sources', 'atoms'}
        for stage in ('sources', 'atoms'):
            entry = report['plans'][stage]
            assert 'error' not in entry, entry
            assert entry['explain'][0]['Plan']['Node Type']
            if analyze:
                assert entry['explain'][0]['Execution Time'] >= 0
    assert await e.db.fetchval('SELECT count(*) FROM aios.entity_memory_projection') > 0


@pytest.mark.asyncio
async def test_query_plan_diagnostic_cannot_write(env, monkeypatch):
    from aios_app.entity_memory.diagnostics import query_plans
    from aios_app.entity_memory.retrieval import _ATOM_SQL
    from test_cognitive_episode_postgres import PGConnection, PGPool
    e = env
    e.db.pool = PGPool(PGConnection(e.db.pool.con.process))
    original = e.db.fetch
    async def attempted_write(sql, *args):
        if sql.startswith('EXPLAIN') and _ATOM_SQL in sql:
            await e.db.execute('DELETE FROM aios.entity_memory_projection')
        return await original(sql, *args)
    monkeypatch.setattr(e.db, 'fetch', attempted_write)
    report = await query_plans(e.db, e.ctx, 'blue mug', analyze=True)
    assert 'read-only' in report['plans']['atoms']['detail'].lower()
    assert await e.db.fetchval('SELECT count(*) FROM aios.entity_memory_projection') > 0
