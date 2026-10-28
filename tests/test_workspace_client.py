"""Workspace protocol and SQL contract tests.

PostgreSQL tests require a disposable database named *_workspace_test via
AIOS_WORKSPACE_TEST_DSN. The fixture replaces its aios schema. Alternatively,
AIOS_WORKSPACE_PGLITE_URL may point to an isolated in-memory PGlite test bridge.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from aios_app.agent.workspaces import (
    AssignmentIn,
    BindingIn,
    FinishIn,
    LeaseIn,
    ObservationIn,
    WorkspaceStore,
    install_workspace_routes,
)


def test_workspace_protocol_requires_separate_key(monkeypatch):
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    monkeypatch.delenv("AIOS_WORKSPACE_API_KEY", raising=False)
    with TestClient(app) as c:
        assert c.get("/workspace-client/protocol").status_code == 503
        monkeypatch.setenv("AIOS_WORKSPACE_API_KEY", "private")
        assert c.get("/workspace-client/protocol").status_code == 401
        assert (
            c.get(
                "/workspace-client/protocol",
                headers={"Authorization": "Bearer private"},
            ).json()["version"]
            == 2
        )


class PGliteDB:
    """Test-only bridge to the PostgreSQL WASM engine; not used in production."""

    def __init__(self, url):
        import httpx

        self.client = httpx.AsyncClient(base_url=url, trust_env=False)
        self.depth = 0

    async def raw(self, sql, args=(), exec=False):
        args = [str(x) if isinstance(x, UUID) else x for x in args]
        r = await self.client.post("/", json={"sql": sql, "args": args, "exec": exec})
        if r.status_code != 200:
            raise RuntimeError(r.text)
        return r.json()

    async def fetch(self, sql, *args):
        d = await self.raw(sql, args)
        rows = d["rows"]
        for row in rows:
            for k, v in row.items():
                if (
                    v
                    and isinstance(v, str)
                    and (k.endswith("_id") or k == "claim_token")
                ):
                    try:
                        row[k] = UUID(v)
                    except ValueError:
                        pass
        return rows

    async def fetchrow(self, sql, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        row = await self.fetchrow(sql, *args)
        return next(iter(row.values())) if row else None

    async def execute(self, sql, *args):
        await self.raw(sql, args)

    execute_returning_row = fetchrow

    @asynccontextmanager
    async def atomic(self):
        name = f"workspace_save_{self.depth}"
        await self.execute("BEGIN" if not self.depth else "SAVEPOINT " + name)
        self.depth += 1
        try:
            yield
        except BaseException:
            self.depth -= 1
            await self.execute(
                "ROLLBACK" if not self.depth else "ROLLBACK TO SAVEPOINT " + name
            )
            raise
        else:
            self.depth -= 1
            await self.execute(
                "COMMIT" if not self.depth else "RELEASE SAVEPOINT " + name
            )


BASE = """
DROP SCHEMA IF EXISTS aios CASCADE;
CREATE SCHEMA aios;
CREATE TABLE aios.session(session_id uuid PRIMARY KEY);
CREATE TABLE aios.character_instance(instance_id uuid PRIMARY KEY,character_id text);
CREATE TABLE aios.timeline(timeline_id uuid PRIMARY KEY,character_id text,session_id uuid,user_name text,scope_key text);
CREATE TABLE aios.character_runtime_state(instance_id uuid PRIMARY KEY,timeline_id uuid,source_timeline_id uuid,source_head_node_id uuid);
CREATE TABLE aios.character_action(action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),instance_id uuid,
 status text DEFAULT 'proposed',action_type text,execution_mode text DEFAULT 'worker',result jsonb,
 started_at timestamptz,completed_at timestamptz,updated_at timestamptz DEFAULT now(),
 task_id uuid,parent_action_id uuid,arguments jsonb DEFAULT '{}',side_effect_class text,
 idempotency_key text,expected_state_version bigint,proposed_by text,meta jsonb DEFAULT '{}',
 error text,rejection_reason text,result_mode text DEFAULT 'final',assigned_worker_id uuid,lease_expires_at timestamptz,
 validated_at timestamptz,queued_at timestamptz,UNIQUE(instance_id,idempotency_key));
CREATE TABLE aios.character_action_policy(instance_id uuid,action_type text,disposition text,PRIMARY KEY(instance_id,action_type));
CREATE TABLE aios.character_action_approval(approval_id uuid DEFAULT gen_random_uuid(),action_id uuid UNIQUE,
 status text DEFAULT 'pending',decided_at timestamptz,decided_by text,reason text);
CREATE TABLE aios.character_agent_runtime(instance_id uuid PRIMARY KEY,state text DEFAULT 'ready',last_wake_at timestamptz,updated_at timestamptz);
CREATE TABLE aios.character_agent_goal(goal_id uuid PRIMARY KEY,instance_id uuid,status text,goal_text text,priority float DEFAULT 1,meta jsonb DEFAULT '{}',completed_at timestamptz,updated_at timestamptz);
CREATE TABLE aios.character_goal_attempt(attempt_id uuid PRIMARY KEY,goal_id uuid REFERENCES aios.character_agent_goal,instance_id uuid,source_timeline_id uuid,closed_at timestamptz);
CREATE TABLE aios.character_wake_event(wake_id uuid DEFAULT gen_random_uuid(),instance_id uuid,event_type text,
 source_type text,source_id text,payload jsonb,priority integer,dedupe_key text,UNIQUE(instance_id,dedupe_key));
"""


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("AIOS_WORKSPACE_PGLITE_URL")
    dsn = os.environ.get("AIOS_WORKSPACE_TEST_DSN")
    if url:
        db = PGliteDB(url)
        await db.raw(BASE, exec=True)
        await db.raw(
            (
                Path(__file__).parents[1]
                / "migrations/current/20261027_01_workspace_client.sql"
            ).read_text(),
            exec=True,
        )
        migration = (
            Path(__file__).parents[1]
            / "migrations/current/20261028_01_workspace_verification.sql"
        ).read_text()
        if url:
            await db.raw(migration, exec=True)
        else:
            await db.execute(migration)
        yield db
        await db.client.aclose()
    elif dsn:
        if not urlsplit(dsn).path.endswith("_workspace_test"):
            raise RuntimeError("Use a disposable database named *_workspace_test")
        from aios_app.db import Database

        db = Database(dsn, min_size=1, max_size=4)
        await db.connect()
        await db.execute(BASE)
        await db.execute(
            (
                Path(__file__).parents[1]
                / "migrations/current/20261027_01_workspace_client.sql"
            ).read_text()
        )
        migration = (
            Path(__file__).parents[1]
            / "migrations/current/20261028_01_workspace_verification.sql"
        ).read_text()
        if url:
            await db.raw(migration, exec=True)
        else:
            await db.execute(migration)
        yield db
        await db.close()
    else:
        pytest.skip("Set AIOS_WORKSPACE_TEST_DSN for PostgreSQL contract tests")


async def binding(db):
    instance, session, runtime_timeline, source_timeline, node = [
        uuid4() for _ in range(5)
    ]
    await db.execute("INSERT INTO aios.session VALUES($1)", session)
    await db.execute(
        "INSERT INTO aios.character_instance VALUES($1,$2)", instance, "Cortana"
    )
    # Runtime and source timelines intentionally differ, as they do in live AIOS.
    for timeline in (runtime_timeline, source_timeline):
        await db.execute(
            "INSERT INTO aios.timeline VALUES($1,$2,$3,$4,$5)",
            timeline,
            "Cortana",
            session,
            "human",
            "workspace",
        )
    await db.execute(
        "INSERT INTO aios.character_runtime_state VALUES($1,$2,$3,$4)",
        instance,
        runtime_timeline,
        source_timeline,
        node,
    )
    req = BindingIn(
        workspace_key=str(uuid4()),
        worker_key="worker",
        character_id="Cortana",
        instance_id=instance,
        session_id=session,
        user_name="human",
        scope_key="workspace",
        root_hint="/project",
    )
    store = WorkspaceStore(db)
    b = await store.bind(req)
    assert b["timeline_id"] == str(source_timeline)
    assert (await store.bind(req))["binding_id"] == b["binding_id"]
    return store, req, UUID(b["binding_id"])


async def enqueue(db, bid, instance, objective="Fix a bug"):
    aid = uuid4()
    await db.execute(
        "INSERT INTO aios.character_action(action_id,instance_id,action_type,status) VALUES($1,$2,'workspace.run','queued')",
        aid,
        instance,
    )
    r = await db.fetchrow(
        "INSERT INTO aios.workspace_run(binding_id,action_id,request_id,objective) VALUES($1,$2,$3,$4) RETURNING run_id",
        bid,
        aid,
        uuid4(),
        objective,
    )
    return r["run_id"]


@pytest.mark.asyncio
async def test_binding_scope_and_source_conflicts(db):
    store, req, bid = await binding(db)
    with pytest.raises(HTTPException):
        await store.bind(req.model_copy(update={"character_id": "Renamon"}))
    with pytest.raises(HTTPException):
        await store.claim(bid, "other-worker")
    await db.execute(
        "UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1",
        req.instance_id,
        uuid4(),
    )
    async with db.atomic():
        with pytest.raises(HTTPException, match="409"):
            await store.current(await store.binding(bid))


@pytest.mark.asyncio
async def test_scoped_claim_renewal_expiry_and_no_replay(db):
    store, req, bid = await binding(db)
    run = await enqueue(db, bid, req.instance_id)
    other, req2, bid2 = await binding(db)
    run2 = await enqueue(db, bid2, req2.instance_id)
    claim = await store.claim(bid, "worker")
    assert claim["run_id"] == str(run) and claim["run_id"] != str(run2)
    assert await store.claim(bid, "worker") is None
    lease = LeaseIn(worker_key="worker", claim_token=claim["claim_token"])
    await store.renew(bid, run, lease)
    with pytest.raises(HTTPException):
        await store.renew(bid, run, lease.model_copy(update={"claim_token": uuid4()}))
    await db.execute(
        "UPDATE aios.workspace_run SET lease_expires_at=now()-interval '1 second' WHERE run_id=$1",
        run,
    )
    with pytest.raises(HTTPException):
        await store.renew(bid, run, lease)
    assert await store.claim(bid, "worker") is None
    assert (
        await db.fetchrow("SELECT status FROM aios.workspace_run WHERE run_id=$1", run)
    )["status"] == "interrupted"
    with pytest.raises(HTTPException):
        await store.finish(
            bid, run, FinishIn(**lease.model_dump(), status="succeeded", result={})
        )


@pytest.mark.asyncio
async def test_finish_retry_is_idempotent_and_wakes_correct_instance(db):
    store, req, bid = await binding(db)
    run = await enqueue(db, bid, req.instance_id)
    claim = await store.claim(bid, "worker")
    finish = FinishIn(
        worker_key="worker",
        claim_token=claim["claim_token"],
        status="succeeded",
        result={"report": "Tests passed", "verification": "worker_report"},
    )
    await store.finish(bid, run, finish)
    await store.finish(bid, run, finish)
    assert (
        await db.fetchval(
            "SELECT count(*) FROM aios.character_wake_event WHERE instance_id=$1",
            req.instance_id,
        )
        == 1
    )
    with pytest.raises(HTTPException):
        await store.finish(
            bid, run, finish.model_copy(update={"result": {"report": "changed"}})
        )


@pytest.mark.asyncio
async def test_tool_observation_is_perceived_once_and_rolls_back_on_conflict(
    db, monkeypatch
):
    import aios_app.ingest_api as ingress
    from aios_app.models import IngestOut

    store, req, bid = await binding(db)
    run = await enqueue(db, bid, req.instance_id)
    claim = await store.claim(bid, "worker")
    seen = []

    async def ingest(actual_db, event):
        seen.append(event)
        assert event.speaker_type == "tool" and event.kind == "tool_result"
        assert event.recipient_id == "Cortana"
        node = uuid4()
        b = await store.binding(bid)
        await db.execute(
            "UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1",
            req.instance_id,
            node,
        )
        return IngestOut(
            ok=True, event_id=1, node_id=node, timeline_id=b["timeline_id"]
        )

    monkeypatch.setattr(ingress, "ingest_message", ingest)
    obs = ObservationIn(
        worker_key="worker",
        claim_token=claim["claim_token"],
        event_key=uuid4(),
        text="Test exited 0",
        payload={"exit_code": 0},
    )
    first = await store.observe(bid, run, obs)
    assert await store.observe(bid, run, obs) == first and len(seen) == 1
    with pytest.raises(HTTPException):
        await store.observe(bid, run, obs.model_copy(update={"text": "changed"}))
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_receipt") == 1
    await db.execute(
        "UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1",
        req.instance_id,
        uuid4(),
    )
    with pytest.raises(HTTPException):
        await store.observe(bid, run, obs.model_copy(update={"event_key": uuid4()}))
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_operator_assignment_dispatches_policy_checked_action(db, monkeypatch):
    from aios_app.agent.lifecycle import CharacterAgencyStore

    async def refresh(self, instance_id):
        pass

    monkeypatch.setattr(CharacterAgencyStore, "_refresh_scene", refresh)
    store, req, bid = await binding(db)
    assignment = AssignmentIn(
        request_id=uuid4(), objective="Fix and test this function"
    )
    first = await store.submit(bid, assignment)
    assert first["status"] == "queued"
    assert await store.submit(bid, assignment) == first
    assert await db.fetchval("SELECT count(*) FROM aios.character_action") == 1
    assert (
        await db.fetchval("SELECT status FROM aios.character_action_approval")
        == "approved"
    )
    with pytest.raises(HTTPException):
        await store.submit(
            bid, assignment.model_copy(update={"objective": "different task"})
        )
    await db.execute(
        "INSERT INTO aios.character_action_policy VALUES($1,'workspace.run','deny')",
        req.instance_id,
    )
    with pytest.raises(HTTPException):
        await store.submit(
            bid, AssignmentIn(request_id=uuid4(), objective="Denied task")
        )
    assert await db.fetchval("SELECT count(*) FROM aios.character_action") == 1


@pytest.mark.asyncio
async def test_session_discovery_returns_only_requested_character(db):
    store, req, bid = await binding(db)
    rows = await store.instances("Cortana")
    assert len(rows) == 1
    assert rows[0]["instance_id"] == str(req.instance_id)
    assert rows[0]["session_id"] == str(req.session_id)
    assert await store.instances("Renamon") == []


def valid_evidence(spec):
    return {
        "assignment_hash": spec.digest(),
        "snapshot_complete": True,
        "snapshot_hash": "a" * 64,
        "final_hash": "a" * 64,
        "outputs": [],
        "violations": [],
        "stale": False,
        "checks": [
            {
                **c.model_dump(),
                "snapshot_hash": "a" * 64,
                "output_complete": True,
                "log_sha256": "b" * 64,
                "exit_code": 0,
                "timed_out": False,
                "cancelled": False,
            }
            for c in spec.required_checks
        ],
    }


async def goal_assignment(db, store, req, bid, monkeypatch):
    from aios_app.agent.lifecycle import CharacterAgencyStore

    async def refresh(*a):
        pass

    monkeypatch.setattr(CharacterAgencyStore, "_refresh_scene", refresh)
    gid, aid = uuid4(), uuid4()
    b = await store.binding(bid)
    await db.execute(
        "INSERT INTO aios.character_agent_goal(goal_id,instance_id,status,goal_text) VALUES($1,$2,'active','Fix bug')",
        gid,
        req.instance_id,
    )
    await db.execute(
        "INSERT INTO aios.character_goal_attempt VALUES($1,$2,$3,$4,NULL)",
        aid,
        gid,
        req.instance_id,
        b["timeline_id"],
    )
    task = AssignmentIn(
        request_id=uuid4(),
        objective="Fix bug",
        goal_id=gid,
        attempt_id=aid,
        required_checks=[{"id": "tests", "command": "python -m unittest"}],
    )
    r = await store.submit(bid, task)
    return task, UUID(r["run_id"])


@pytest.mark.asyncio
async def test_criteria_immutable_retry_and_worker_report_not_goal_completion(
    db, monkeypatch
):
    from aios_app.agent.workspaces import ReviewIn

    store, req, bid = await binding(db)
    task, rid = await goal_assignment(db, store, req, bid, monkeypatch)
    assert (await store.submit(bid, task))["assignment_hash"] == task.spec().digest()
    with pytest.raises(HTTPException):
        await store.submit(bid, task.model_copy(update={"required_checks": []}))
    with pytest.raises(Exception, match="immutable"):
        async with db.atomic():
            await db.execute(
                "UPDATE aios.workspace_run SET assignment='{}' WHERE run_id=$1", rid
            )
    claim = await store.claim(bid, "worker")
    finish = FinishIn(
        worker_key="worker",
        claim_token=claim["claim_token"],
        status="succeeded",
        result={"evidence": valid_evidence(task.spec()), "report": "done"},
    )
    first = await store.finish(bid, rid, finish)
    assert first["status"] == "finished"
    assert first["result"]["verification_status"] == "passed"
    assert (
        await db.fetchval(
            "SELECT status FROM aios.character_agent_goal WHERE goal_id=$1",
            task.goal_id,
        )
        == "active"
    )
    assert (
        await db.fetchval(
            "SELECT status FROM aios.workspace_review WHERE run_id=$1", rid
        )
        == "pending"
    )
    assert await store.finish(bid, rid, finish) == first
    review = ReviewIn(
        accept=True, reviewer="human", reason="Reviewed changes and check logs"
    )
    assert (await store.review(bid, rid, review))["status"] == "accepted"
    assert (await store.review(bid, rid, review))["status"] == "accepted"
    meta = await db.fetchval(
        "SELECT meta FROM aios.character_agent_goal WHERE goal_id=$1", task.goal_id
    )
    import json

    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta["outcome_decision"]["verification"] == "reviewed"


@pytest.mark.asyncio
async def test_spoofed_pass_cannot_be_accepted_and_closed_attempt_cannot_finish_goal(
    db, monkeypatch
):
    from aios_app.agent.workspaces import ReviewIn

    store, req, bid = await binding(db)
    task, rid = await goal_assignment(db, store, req, bid, monkeypatch)
    claim = await store.claim(bid, "worker")
    finish = FinishIn(
        worker_key="worker",
        claim_token=claim["claim_token"],
        status="finished",
        result={"evidence": {"status": "passed"}, "verification_status": "passed"},
    )
    row = await store.finish(bid, rid, finish)
    assert row["result"]["verification_status"] == "incomplete"
    with pytest.raises(HTTPException):
        await store.review(
            bid, rid, ReviewIn(accept=True, reviewer="human", reason="try")
        )
    await db.execute(
        "UPDATE aios.character_goal_attempt SET closed_at=now() WHERE attempt_id=$1",
        task.attempt_id,
    )
    with pytest.raises(HTTPException):
        await store.submit(bid, task.model_copy(update={"request_id": uuid4()}))
    assert (
        await store.review(
            bid,
            rid,
            ReviewIn(accept=False, reviewer="human", reason="Incomplete evidence"),
        )
    )["status"] == "rejected"
    assert (
        await db.fetchval(
            "SELECT status FROM aios.character_agent_goal WHERE goal_id=$1",
            task.goal_id,
        )
        == "active"
    )


@pytest.mark.asyncio
async def test_passed_old_attempt_and_foreign_goal_cannot_resolve_new_goal(
    db, monkeypatch
):
    from aios_app.agent.workspaces import ReviewIn

    store, req, bid = await binding(db)
    task, rid = await goal_assignment(db, store, req, bid, monkeypatch)
    foreign, foreign_req, foreign_bid = await binding(db)
    with pytest.raises(HTTPException):
        await store.submit(foreign_bid, task.model_copy(update={"request_id": uuid4()}))
    claim = await store.claim(bid, "worker")
    await store.finish(
        bid,
        rid,
        FinishIn(
            worker_key="worker",
            claim_token=claim["claim_token"],
            status="finished",
            result={"evidence": valid_evidence(task.spec())},
        ),
    )
    await db.execute(
        "UPDATE aios.character_goal_attempt SET closed_at=now() WHERE attempt_id=$1",
        task.attempt_id,
    )
    with pytest.raises(HTTPException):
        await store.review(
            bid, rid, ReviewIn(accept=True, reviewer="human", reason="Too late")
        )
    assert (
        await db.fetchval(
            "SELECT status FROM aios.character_agent_goal WHERE goal_id=$1",
            task.goal_id,
        )
        == "active"
    )


def test_review_requires_distinct_credential(monkeypatch):
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    monkeypatch.setenv("AIOS_WORKSPACE_API_KEY", "worker")
    monkeypatch.setenv("AIOS_WORKSPACE_REVIEW_KEY", "review")
    with TestClient(app) as c:
        path = f"/workspace-client/{uuid4()}/runs/{uuid4()}/review"
        assert (
            c.post(
                path,
                headers={"Authorization": "Bearer worker"},
                json={"accept": True, "reviewer": "fake", "reason": "x"},
            ).status_code
            == 401
        )
        monkeypatch.setenv("AIOS_WORKSPACE_REVIEW_KEY", "worker")
        assert (
            c.post(
                path,
                headers={"Authorization": "Bearer worker"},
                json={"accept": True, "reviewer": "fake", "reason": "x"},
            ).status_code
            == 503
        )
