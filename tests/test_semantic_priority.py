import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aios_app.semantic_index import cli, service, structure, neighbor_classifier, admission, eligibility
from aios_app.semantic_index.config import SemanticIndexConfig
from aios_app.semantic_index.relation_validator import has_structural_anchor


@pytest.mark.asyncio
async def test_embedding_cycle_has_no_topology_dependency_and_newest_vectors_first(monkeypatch):
    calls = []
    monkeypatch.setattr(cli,"_get_embedder",lambda cfg:object())
    monkeypatch.setattr(cli,"_get_store",lambda cfg,c:object())

    async def stage(name,func,*args,**kwargs):
        calls.append((name,args[1].batch_size))
        return 0

    cfg=SemanticIndexConfig()
    await cli._run_vector_stages(None,cfg,stage)
    assert [n for n,_ in calls] == [
        "vector-propositions","semantic-admission","semantic-admission-repair",
        "vector-frames","vector-epistemic","vector-source","vector-corpus"]
    assert calls[1][1] == cfg.admission_batch_size
    assert calls[-1][1] == cfg.background_batch_size


@pytest.mark.asyncio
async def test_cold_corpus_yields_when_live_embedding_batch_is_full(monkeypatch):
    calls=[]
    monkeypatch.setattr(cli,"_get_embedder",lambda cfg:None)
    monkeypatch.setattr(cli,"_get_store",lambda cfg,c:None)
    cfg=SemanticIndexConfig()
    async def stage(name,func,*args,**kwargs):
        calls.append(name)
        return cfg.batch_size if name=="vector-propositions" else 0
    await cli._run_vector_stages(None,cfg,stage)
    assert "vector-corpus" not in calls
    assert "vector-source" in calls


def test_launcher_starts_independent_topology_after_vector_backend():
    from aios_app.launch import SERVICES,STARTUP_STAGES
    specs={s["name"]:s for s in SERVICES}
    assert "--topology-only" in specs["Semantic Topology"]["cmd"]
    assert "--topology-only" not in specs["Semantic Index"]["cmd"]
    stages=[names for _,names in STARTUP_STAGES]
    assert stages.index({"Semantic Topology"}) > stages.index({"Semantic Index"})


@pytest.mark.asyncio
async def test_slow_topology_yields_and_validation_still_runs(monkeypatch):
    calls=[]
    closed=[]
    class DB:
        def __init__(self,*args,**kwargs):
            assert kwargs["max_size"] == 2
            assert kwargs["server_settings"]["statement_timeout"] == "5000"
        async def connect(self): pass
        async def close(self): closed.append(True)
    monkeypatch.setattr(cli,"Database",DB)
    monkeypatch.setattr(cli,"SemanticIndexConfig",lambda:replace(SemanticIndexConfig(),topology_stage_seconds=.01))
    monkeypatch.setattr(cli,"initialize_topology_backend",lambda cfg:None)
    async def slow(*args):
        calls.append("classifier")
        await asyncio.Event().wait()
    monkeypatch.setattr(cli,"classify_neighbor_relations_once",slow)
    async def quick(*args,**kwargs):
        calls.append("other")
        return 0
    for name in ["analyze_neighbors_once","record_new_relation_decisions",
                 "reconcile_neighbor_relations_once","retract_superseded_clusters_once",
                 "reproject_pending_reconciliation_once","quarantine_ineligible_vectors_once"]:
        monkeypatch.setattr(cli,name,quick)
    async def stop(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(cli.asyncio,"sleep",stop)
    with pytest.raises(asyncio.CancelledError):
        await cli.run_topology_forever()
    assert calls[0:2] == ["other","classifier"]
    assert calls[2:] == ["other"]*5
    assert closed == [True]


def test_topology_uses_existing_dimensions_without_loading_model(monkeypatch):
    import qdrant_client
    clients=[]
    class Client:
        def __init__(self,**kwargs): clients.append(kwargs)
        def get_collection(self,c):
            return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=768))))
        def close(self): pass
    monkeypatch.setattr(qdrant_client,"QdrantClient",Client)
    monkeypatch.setattr(service,"QdrantStore",lambda *args:args)
    monkeypatch.setattr(service,"_STORES",{})
    def forbidden(*args): raise AssertionError("extra embedding model")
    monkeypatch.setattr(service,"_get_embedder",forbidden)
    cfg=SemanticIndexConfig()
    service.initialize_topology_backend(cfg)
    assert service._STORES[cfg.proposition_collection][-1] == 768
    assert clients[0]["timeout"] == 5


class EmptyDB:
    def __init__(self): self.queries=[]
    async def fetch(self,sql,*args):
        self.queries.append(sql)
        return []


@pytest.mark.asyncio
async def test_all_embedding_streams_choose_newest_pending_records():
    cfg=SemanticIndexConfig()
    db=EmptyDB()
    for f in [service.index_propositions_once,service.index_semantic_frames_once,
              service.index_epistemic_objects_once,service.index_source_sections_once,
              service.index_corpus_sections_once]:
        assert await f(db,cfg) == 0
    assert all("DESC" in q[q.rindex("ORDER BY"):] for q in db.queries)
    frames=db.queries[1]
    assert "f.resolution_status='resolved'" in frames and "si.standalone_semantic" in frames


def test_candidate_gate_preserves_opposition_and_role_reversal():
    a={"subject_norm":"bob","object_norm":"salad","topic_key":"food"}
    assert has_structural_anchor(a,dict(a,polarity=-1))
    assert has_structural_anchor(a,{"subject_norm":"salad","object_norm":"bob"})
    assert not has_structural_anchor(a,{"subject_norm":"alice","object_norm":"engine","topic_key":"mechanics"})


@pytest.mark.asyncio
async def test_neighbor_search_prunes_before_inserting_and_ignores_unchanged_hits(monkeypatch):
    p,good,bad=uuid4(),uuid4(),uuid4()
    row={"proposition_id":p,"subject_norm":"bob","object_norm":"salad","topic_key":"food","admitted":True}
    peer=dict(row,proposition_id=good)
    unrelated=dict(row,proposition_id=bad,subject_norm="alice",object_norm="engine",topic_key="mechanics")
    class DB:
        def __init__(self):self.inserts=[];self.executed=[];self.reads=0
        async def fetch(self,sql,*args):
            self.reads+=1
            return [row] if self.reads==1 else [peer,unrelated]
        async def fetchrow(self,sql,*args):
            self.inserts.append((sql,args));return None
        async def execute(self,sql,*args):self.executed.append(sql)
    class Store:
        def vector(self,p):return [1.0]
        def search(self,v,**kwargs):
            assert kwargs["score_threshold"] == .72
            return [(str(good),.95,{"proposition_id":str(good)}),(str(bad),.99,{"proposition_id":str(bad)})]
    monkeypatch.setattr(structure,"_get_store",lambda *args:Store())
    db=DB()
    assert await structure.analyze_neighbors_once(db,SemanticIndexConfig()) == 0
    assert len(db.inserts) == 1
    assert str(good) in db.inserts[0][1]
    assert "similarity < EXCLUDED.similarity" in db.inserts[0][0]
    assert any("analyzed_at=now()" in q for q in db.executed)


@pytest.mark.asyncio
async def test_classifier_shortlists_before_context_and_culls_unsupported_pairs():
    a,b=uuid4(),uuid4()
    class DB:
        def __init__(self): self.query="";self.executed=[]
        async def fetch(self,sql,*args):
            self.query=sql
            return [{"proposition_id":a,"neighbor_proposition_id":b,"structural_supported":False}]
        async def execute(self,sql,*args):self.executed.append(sql)
    db=DB()
    assert await neighbor_classifier.classify_neighbor_relations_once(db,SemanticIndexConfig()) == 0
    assert db.query.index("LIMIT $3") < db.query.index("LEFT JOIN LATERAL")
    assert "AS MATERIALIZED" in db.query
    assert "last_classifier_version IS DISTINCT FROM $2" in db.query
    assert any("status='culled'" in q for q in db.executed)


@pytest.mark.asyncio
async def test_unadmitted_pair_defers_without_fabricating_a_relation():
    class DB:
        def __init__(self):self.executed=[]
        async def fetch(self,*args):
            return [{"proposition_id":uuid4(),"neighbor_proposition_id":uuid4(),
                     "structural_supported":True,"a_claim_id":None,"b_claim_id":None}]
        async def execute(self,sql,*args):self.executed.append(sql)
    db=DB()
    assert await neighbor_classifier.classify_neighbor_relations_once(db,SemanticIndexConfig()) == 0
    assert "classification_ready_at" in db.executed[0]
    assert not any("INSERT" in q for q in db.executed)


@pytest.mark.asyncio
async def test_admission_batch_embeds_once_and_preserves_scope(monkeypatch):
    rows=[{"claim_id":uuid4(),"proposition_id":uuid4(),"canonical_text":"bob likes salad","scope_key":"character:x"} for _ in range(2)]
    class DB:
        async def fetch(self,*args):return rows
    texts=[]
    class Embedder:
        def embed(self,values):texts.append(values);return [[1.0],[2.0]]
    seen=[]
    def search(**kwargs):
        seen.append(kwargs["vector"]);return []
    async def classify(db,**kwargs):
        assert kwargs["scope_key"]=="character:x"
        return admission.AdmissionDecision("novel",None,None,"test")
    recorded=[]
    async def record(db,**kwargs):recorded.append(kwargs["claim_id"])
    monkeypatch.setattr(admission,"search_canonical_candidates",search)
    monkeypatch.setattr(admission,"classify_semantic_admission",classify)
    monkeypatch.setattr(admission,"_record_decision",record)
    assert await admission.admit_semantic_neighbors_once(DB(),SemanticIndexConfig(),embedder=Embedder(),store=None) == 2
    assert len(texts)==1 and len(texts[0])==2
    assert seen==[[1.0],[2.0]] and len(recorded)==2


@pytest.mark.asyncio
async def test_vector_mutation_serializes_receipt_after_acknowledged_upsert(monkeypatch):
    events=[];p=uuid4()
    class Con:
        @asynccontextmanager
        async def transaction(self):yield self
        async def execute(self,sql,*args):
            events.append("lock" if "advisory" in sql else "receipt")
        async def fetch(self,*args):events.append("eligibility");return [{"proposition_id":p}]
    class DB:
        @asynccontextmanager
        async def connection(self):yield Con()
    class Store:
        def upsert(self,points):events.append("upsert")
    monkeypatch.setattr(service,"_get_store",lambda *args:Store())
    count=await service._write_proposition_points(DB(),SemanticIndexConfig(),"props",
        [{"proposition_id":p}],[object()],["hash"])
    assert count==1 and events==["lock","eligibility","upsert","receipt"]


@pytest.mark.asyncio
async def test_failed_upsert_never_earns_an_index_receipt(monkeypatch):
    events=[];p=uuid4()
    class Con:
        @asynccontextmanager
        async def transaction(self):yield self
        async def execute(self,sql,*args):events.append(sql)
        async def fetch(self,*args):return [{"proposition_id":p}]
    class DB:
        @asynccontextmanager
        async def connection(self):yield Con()
    class Store:
        def upsert(self,points):raise RuntimeError("Qdrant unavailable")
    monkeypatch.setattr(service,"_get_store",lambda *args:Store())
    with pytest.raises(RuntimeError):
        await service._write_proposition_points(DB(),SemanticIndexConfig(),"props",
            [{"proposition_id":p}],[object()],["hash"])
    assert not any("INSERT INTO aios.semantic_vector_index_state" in q for q in events)


@pytest.mark.asyncio
async def test_quarantine_yields_its_vector_lock_to_indexing(monkeypatch):
    p=uuid4()
    class Con:
        @asynccontextmanager
        async def transaction(self):yield self
        async def fetchval(self,sql,*args):return False
    class DB:
        async def execute(self,*args):pass
        async def fetch(self,*args):return [{"proposition_id":p}]
        @asynccontextmanager
        async def connection(self):yield Con()
    def forbidden(*args):raise AssertionError("must not delete indexing vectors")
    monkeypatch.setattr(eligibility,"_get_store",forbidden)
    assert await eligibility.quarantine_ineligible_vectors_once(DB(),SemanticIndexConfig())==0


@pytest.mark.asyncio
async def test_budget_failure_halves_next_classifier_batch(monkeypatch):
    sizes=[];slept=[]
    class DB:
        def __init__(self,*args,**kwargs):pass
        async def connect(self):pass
        async def close(self):pass
    monkeypatch.setattr(cli,"Database",DB)
    monkeypatch.setattr(cli,"initialize_topology_backend",lambda cfg:None)
    async def classifier(db,cfg):
        sizes.append(cfg.relation_batch_size)
        if len(sizes)==1:
            raise cli.asyncpg.QueryCanceledError("SQL budget")
        return 0
    monkeypatch.setattr(cli,"classify_neighbor_relations_once",classifier)
    async def quick(*args,**kwargs):return 0
    for name in ["analyze_neighbors_once","record_new_relation_decisions",
                 "reconcile_neighbor_relations_once","retract_superseded_clusters_once",
                 "reproject_pending_reconciliation_once","quarantine_ineligible_vectors_once"]:
        monkeypatch.setattr(cli,name,quick)
    async def stop(seconds):
        slept.append(seconds)
        if len(slept)==2:
            raise asyncio.CancelledError
    monkeypatch.setattr(cli.asyncio,"sleep",stop)
    with pytest.raises(asyncio.CancelledError):
        await cli.run_topology_forever()
    cfg=SemanticIndexConfig()
    assert sizes==[cfg.relation_batch_size,max(1,cfg.relation_batch_size//2)]
    assert slept[0]>=5


@pytest.mark.asyncio
async def test_batch_admission_preserves_opposite_polarity_challenge():
    p,other,claim=uuid4(),uuid4(),uuid4()
    current={"subject_norm":"bob","predicate_norm":"likes","object_norm":"salad",
             "polarity":1,"modality":"asserted","topic_key":"food"}
    class DB:
        async def fetchrow(self,*args):return current
        async def fetch(self,sql,*args):
            assert args[1]=="character:x"
            assert "exact_admission_scope_key(o.claim_id)=$2" in sql
            return [dict(current,proposition_id=other,matched_claim_id=claim,polarity=-1)]
    decision=await admission.classify_semantic_admission(DB(),proposition_id=p,
        scope_key="character:x",candidates=[admission.AdmissionCandidate(other,.95)])
    assert decision.decision=="challenges"
    assert decision.matched_claim_id==claim


@pytest.mark.asyncio
async def test_quarantine_rechecks_eligibility_after_acquiring_lock(monkeypatch):
    p=uuid4()
    class Con:
        @asynccontextmanager
        async def transaction(self):yield self
        async def fetchval(self,*args):return True
        async def fetch(self,*args):return []  # Became eligible while cleanup queued.
    class DB:
        async def execute(self,*args):pass
        async def fetch(self,*args):return [{"proposition_id":p}]
        @asynccontextmanager
        async def connection(self):yield Con()
    def forbidden(*args):raise AssertionError("new eligible vector must survive")
    monkeypatch.setattr(eligibility,"_get_store",forbidden)
    assert await eligibility.quarantine_ineligible_vectors_once(DB(),SemanticIndexConfig())==0
