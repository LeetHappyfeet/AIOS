import asyncio
from aios_app.agent.participation import propose_v2, ParticipationService
from aios_app.tests.test_participation_shadow import context, claim, Connection, Database, item


def evidence(**changes):
    return claim(predicate_norm="water", source_sentence="Alice waters flowers",
                 dag_node_id="node", speaker_id="Alice", target_character_id="renamon", **changes)


def test_source_routing_is_not_significance():
    result = propose_v2(evidence(), context(), recurrence=1)
    assert result["population"] == "latent"
    assert result["signals"]["involvement"]["source_target"]
    assert not result["signals"]["involvement"]["claim_participation"]
    assert result["signals"]["involvement"]["addressed"] is None


def test_participation_needs_significance_or_independent_repeat():
    c = evidence(subject_norm="renamon")
    assert propose_v2(c, context(), recurrence=1)["population"] == "latent"
    assert propose_v2(c, context(), recurrence=3)["population"] == "foreground"


def test_generic_recurrence_and_broken_frames_stay_latent():
    for c in [evidence(subject_norm="nothing", object_norm="_"),
              evidence(subject_norm="_", object_norm="_"),
              evidence(subject_norm="renamon", object_norm="_")]:
        r = propose_v2(c, context(), recurrence=4)
        assert r["population"] == "latent"
        assert not r["signals"]["representation_quality"]["usable"]


def test_valid_intransitive_can_be_relevant():
    c = claim(canonical_text="Renamon hesitates", subject_norm="renamon", predicate_norm="hesitate", object_norm="_")
    ctx = context(goals=[{"goal_id": "g", "goal_text": "Renamon hesitates before accepting"}])
    assert propose_v2(c, ctx, recurrence=1)["population"] == "foreground"


def test_independent_repetition_alone_does_not_establish_significance():
    assert propose_v2(evidence(), context(), recurrence=4)["population"] == "latent"


def test_legacy_replay_does_not_invent_missing_evidence():
    r = propose_v2(claim(), context())
    assert r["population"] == "latent"
    assert "independent_recurrence" in r["signals"]["unknown"]
    assert "speaker_id" in r["signals"]["missing_snapshot_fields"]


def test_context_audit_reports_exclusions_and_preserves_scope():
    class AuditConnection(Connection):
        async def fetch(self, sql, *args):
            self.calls.append((sql,args))
            if "character_agent_goal" in sql:
                return [{"goal_id":"g", "goal_text":"study rare flowers", "status":"scheduled"}]
            if "character_identity_facet" in sql:
                return [{"facet_id":"f", "facet_type":"domain", "value":"fiction.digimon"}]
            if "character_identity_candidate" in sql:
                return [{"candidate_id":"c", "facet_type":"personality", "disposition":"proposed"}]
            return [{"relationship_id":"r", "entity_key":None,"display_name":None}]
    con=AuditConnection()
    ctx=asyncio.run(ParticipationService(Database(con))._context(con,item()))
    assert not ctx["goals"] and not ctx["facets"] and not ctx["relationships"]
    assert ctx["coverage"]["goals"]["sampled_rows"][0]["status"] == "scheduled"
    assert ctx["coverage"]["facets"]["excluded_types"] == ["domain"]
    assert ctx["coverage"]["facets"]["candidate_sample"][0]["disposition"] == "proposed"
    assert ctx["coverage"]["relationships"]["missing_entities"] == 1
    assert "WHERE instance_id=$1" in con.calls[0][0]


def test_recurrence_query_deduplicates_source_occurrences():
    con=Connection(item(predicate_norm="water", source_sentence="Alice waters flowers",dag_node_id="node",source_key="source",speaker_id="Alice",target_character_id="renamon"))
    assert asyncio.run(ParticipationService(Database(con)).process_pending(limit=1)) == 1
    assert any("SELECT DISTINCT COALESCE(o.dag_node_id::text,o.document_id::text)" in sql for sql,_ in con.calls)


def test_replay_preserves_baseline_and_frozen_context():
    import json
    class ReplayConnection(Connection):
        async def fetch(self, sql, *args):
            return [{"claim_id":"claim", "signals":json.dumps({"conflict_ids":[]}),
                     "claim_snapshot":json.dumps(claim()),"context_snapshot":json.dumps(context())}]
    con=ReplayConnection()
    result=asyncio.run(ParticipationService(Database(con)).compare_existing("instance","experiment"))
    assert result["compared"] == 1
    update=next(sql for sql,_ in con.calls if "UPDATE" in sql)
    assert "SET signals=jsonb_set" in update
    assert "SET population" not in update
    assert not any("character_agent_goal" in sql for sql,_ in con.calls)


def test_context_and_compare_api_scope(monkeypatch):
    from uuid import uuid4
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aios_app.agent.api import install_external_agency_routes
    instance, experiment = uuid4(), uuid4()
    app = FastAPI()
    install_external_agency_routes(app, None)
    calls = []
    async def audit(self, instance_id):
        calls.append((instance_id,))
        return {"shadow": True, "context": {"goals": []}}
    async def compare(self, instance_id, experiment_id, **kwargs):
        calls.append((instance_id, experiment_id, kwargs))
        return {"shadow": True, "compared": 1}
    monkeypatch.setattr(ParticipationService, "audit_context", audit)
    monkeypatch.setattr(ParticipationService, "compare_existing", compare)
    client = TestClient(app)
    base = f"/agent/instance/{instance}/participation"
    assert client.get(base + "/context").status_code == 200
    assert client.post(base + f"/experiments/{experiment}/compare?limit=10").json()["compared"] == 1
    assert calls == [(instance,), (instance, experiment, {"limit": 10})]
