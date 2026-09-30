import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

from aios_app.agent.participation import ParticipationService, propose


def context(**changes):
    return {"names": ["renamon"], "goals": [], "relationships": [],
            "facets": [{"facet_id": "identity", "value": "protect autonomy"}],
            "truncated": {}, **changes}


def claim(**changes):
    return {"canonical_text": "Alice waters flowers", "subject_norm": "alice",
            "object_norm": "flowers", **changes}


def test_character_context_changes_decision():
    c = claim(canonical_text="Alice protects autonomy")
    assert propose(c, context())["population"] == "foreground"
    assert propose(c, context(facets=[{"facet_id": "other", "value": "study stars"}]))["population"] == "background"


def test_missing_or_truncated_context_is_latent():
    assert propose(claim(), context(facets=[]))["population"] == "latent"
    result = propose(claim(), context(truncated={"goals": True}))
    assert result["population"] == "latent"
    assert "goals_truncated" in result["signals"]["unknown"]


def test_json_keys_do_not_create_relevance():
    ctx = context(facets=[{"facet_id": "f", "value": '{"flowers":"study stars"}'}])
    assert propose(claim(), ctx)["population"] == "background"


def test_goal_match_requires_two_nontrivial_tokens():
    ctx = context(goals=[{"goal_id": "g", "goal_text": "study rare flowers"}])
    assert propose(claim(), ctx)["population"] == "background"
    result = propose(claim(canonical_text="Alice studies rare flowers"), ctx)
    assert result["population"] == "foreground"
    assert result["signals"]["goal_ids"] == ["g"]


def test_direct_involvement_and_relationship_are_distinct():
    assert propose(claim(target_character_id="renamon"), context())["population"] == "foreground"
    result = propose(claim(), context(relationships=[
        {"relationship_id": "r", "display_name": "Alice", "entity_key": "person:alice"}]))
    assert result["population"] == "latent"
    assert result["signals"]["relationship_ids"] == ["r"]


def test_recurrence_and_conflict_are_attention_hints():
    assert propose(claim(), context(), recurrence=2)["population"] == "latent"
    assert propose(claim(), context(), recurrence=3)["population"] == "foreground"
    result = propose(claim(), context(), conflicts=["conflict"])
    assert result["population"] == "foreground"
    assert result["reasons"] == ["known_conflict_candidate"]


class Connection:
    def __init__(self, item=None, fail_context=False):
        self.item = item
        self.fail_context = fail_context
        self.calls = []
        self.picked = False

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self.picked:
            return None
        self.picked = True
        return self.item

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail_context:
            raise RuntimeError("test database failure")
        return []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return 1


class Database:
    def __init__(self, con):
        self.con = con

    @asynccontextmanager
    async def connection(self):
        yield self.con


def item(**changes):
    now = datetime.now(timezone.utc)
    return {"experiment_id": uuid4(), "claim_id": uuid4(), "instance_id": uuid4(),
            "until_at": now + timedelta(hours=1), "queued_at": now, "character_id": "renamon",
            "display_name": "Renamon", "canonical_name": "Renamon",
            "proposition_id": uuid4(), "observed_at": now, "resolved_at": now,
            "claim_kind": "FACT", "epistemic_status": "observed",
            "acquired_claim": uuid4(), **claim(), **changes}


def test_worker_skips_withdrawn_visibility_without_reading_context():
    con = Connection(item(acquired_claim=None))
    assert asyncio.run(ParticipationService(Database(con)).process_pending()) == 0
    assert any("visibility withdrawn" in sql for sql, _ in con.calls)
    assert not any("character_agent_goal" in sql for sql, _ in con.calls)


@pytest.mark.parametrize("changes", [{"claim_kind": "UNKNOWN"}, {"proposition_id": None}])
def test_unresolved_evidence_is_deferred(changes):
    con = Connection(item(**changes))
    asyncio.run(ParticipationService(Database(con)).process_pending())
    assert any("awaiting resolved context" in sql for sql, _ in con.calls)
    assert not any("INSERT INTO aios.character_participation_evaluation" in sql for sql, _ in con.calls)


def test_worker_ledger_is_scoped_and_has_auditable_inputs():
    con = Connection(item())
    assert asyncio.run(ParticipationService(Database(con)).process_pending(limit=1)) == 1
    ledger = next(args for sql, args in con.calls if "INSERT INTO aios.character_participation_evaluation" in sql)
    assert ledger[2] == con.item["instance_id"]
    assert json.loads(ledger[8])["canonical_text"] == "Alice waters flowers"
    assert json.loads(ledger[7])["unknown"] == ["identity_relevance"]
    assert all("qdrant" not in sql.lower() and "semantic_neighbor" not in sql.lower() for sql, _ in con.calls)
    assert any("ck.instance_id=$1" in sql and "o.observed_at<=$3" in sql for sql, _ in con.calls)


def test_worker_failure_retries_are_capped():
    con = Connection(item(), fail_context=True)
    assert asyncio.run(ParticipationService(Database(con)).process_pending()) == 0
    assert any("error_count>=2" in sql and args[-1] == "RuntimeError" for sql, args in con.calls)


def test_worker_batch_limit():
    class AlwaysReady(Connection):
        async def fetchrow(self, sql, *args):
            return self.item
    con = AlwaysReady(item())
    assert asyncio.run(ParticipationService(Database(con)).process_pending(limit=2)) == 2


@pytest.mark.parametrize("kwargs", [
    {"max_claims": 1001}, {"duration_minutes": 0},
    {"since_at": datetime.now()}, {"since_at": datetime.now(timezone.utc)-timedelta(days=8)}
])
def test_invalid_enrollment_rejected_before_database_work(kwargs):
    con = Connection()
    with pytest.raises(ValueError):
        asyncio.run(ParticipationService(Database(con)).start(uuid4(), **kwargs))
    assert not con.calls


def test_api_enrollment_validation_and_scope(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aios_app.agent.api import install_external_agency_routes

    app = FastAPI()
    install_external_agency_routes(app, None)
    instance, exp = uuid4(), uuid4()
    base = f"/agent/instance/{instance}/participation/experiments"
    calls = []

    async def start(self, instance_id, **kwargs):
        calls.append((instance_id, kwargs))
        return {"experiment_id": exp, "shadow": True}

    async def inspect(self, instance_id, experiment_id, **kwargs):
        assert instance_id == instance and experiment_id == exp
        raise LookupError("Experiment not found for instance")

    monkeypatch.setattr(ParticipationService, "start", start)
    monkeypatch.setattr(ParticipationService, "inspect", inspect)
    client = TestClient(app)
    assert client.post(base, json={"max_claims": 1001}).status_code == 422
    assert not calls
    response = client.post(base, json={"max_claims": 5})
    assert response.status_code == 200
    assert response.json()["shadow"] is True
    assert calls[0][0] == instance
    assert calls[0][1]["max_claims"] == 5
    assert client.get(base + "/" + str(exp)).status_code == 404


def test_api_stop_and_invalid_date(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aios_app.agent.api import install_external_agency_routes

    app = FastAPI()
    install_external_agency_routes(app, None)
    instance, exp = uuid4(), uuid4()
    base = f"/agent/instance/{instance}/participation/experiments"
    calls = []

    async def stop(self, instance_id, experiment_id):
        calls.append((instance_id, experiment_id))

    monkeypatch.setattr(ParticipationService, "stop", stop)
    client = TestClient(app)
    # Real start method rejects naive timestamps before touching the database.
    assert client.post(base, json={"since_at": "2026-09-30T00:00:00"}).status_code == 422
    assert client.post(base + "/" + str(exp) + "/stop").json() == {"shadow": True, "status": "stopped"}
    assert calls == [(instance, exp)]
