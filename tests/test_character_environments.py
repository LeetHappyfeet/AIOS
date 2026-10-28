"""Concurrent source streams, explicit memory sharing, and delegated lifecycle.

SQL tests use the same isolated engine/fixture as workspace protocol tests.
"""

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import test_workspace_client as workspace_tests
from aios_app.agent.environment_links import (
    EnvironmentStore,
    LinkIn,
    ProgressIn,
    RecallIn,
    task_overlay,
)
from aios_app.agent.workspaces import (
    DelegationIn,
    FinishIn,
    LeaseIn,
    ReviewIn,
    install_workspace_routes,
)
from aios_app.hud.render_text import render_hud_text
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_workspace_client import binding, valid_evidence

db = workspace_tests.db


@pytest.fixture(autouse=True)
def isolated_scene_projection(monkeypatch):
    # These SQL fixtures omit the independent scene graph. Lifecycle SQL,
    # policies, leases, events and goal ownership still execute normally.
    from aios_app.agent.lifecycle import CharacterAgencyStore

    monkeypatch.setattr(CharacterAgencyStore, "_refresh_scene", AsyncMock())


async def pair(db, share=True):
    store, origin, _obid = await binding(db)
    _, execution, bid = await binding(db)
    for actor in (origin, execution):
        state = await db.fetchrow(
            "SELECT * FROM aios.character_runtime_state WHERE instance_id=$1",
            actor.instance_id,
        )
        await db.execute(
            "INSERT INTO aios.dag_node VALUES($1,$2,1)",
            state["source_head_node_id"],
            state["source_timeline_id"],
        )
    link = await EnvironmentStore(db).create(
        bid, LinkIn(origin_instance_id=origin.instance_id, share_memory=share)
    )
    return store, origin, execution, bid, UUID(link["link_id"])


async def request(db, origin, **extra):
    node = await db.fetchval(
        "SELECT source_head_node_id FROM aios.character_runtime_state WHERE instance_id=$1",
        origin.instance_id,
    )
    return DelegationIn(
        request_id=uuid4(),
        source_node_id=node,
        objective="Fix service authentication",
        **extra,
    )


async def scope(db, instance):
    return {
        r["instance_id"]
        for r in await db.fetch(
            "SELECT instance_id FROM aios.cognitive_evidence_instances($1)", instance
        )
    }


@pytest.mark.asyncio
async def test_memory_is_explicit_direct_and_revocable(db):
    _store, origin, execution, bid, link = await pair(db)
    _, unrelated, _ = await binding(db)
    assert await scope(db, origin.instance_id) == {
        origin.instance_id,
        execution.instance_id,
    }
    assert await scope(db, execution.instance_id) == {
        origin.instance_id,
        execution.instance_id,
    }
    assert await scope(db, unrelated.instance_id) == {unrelated.instance_id}
    # A-B and B-C must not implicitly grant A access to C.
    await EnvironmentStore(db).create(
        bid, LinkIn(origin_instance_id=unrelated.instance_id, share_memory=True)
    )
    assert unrelated.instance_id not in await scope(db, origin.instance_id)
    await EnvironmentStore(db).revoke(bid, link)
    assert await scope(db, origin.instance_id) == {origin.instance_id}
    assert origin.instance_id not in await scope(db, execution.instance_id)


@pytest.mark.asyncio
async def test_identity_user_and_isolated_policy_do_not_grant_access(db):
    _, origin, execution, bid, _link = await pair(db, share=False)
    assert await scope(db, execution.instance_id) == {execution.instance_id}
    with pytest.raises(HTTPException):
        await EnvironmentStore(db).create(
            bid, LinkIn(origin_instance_id=execution.instance_id)
        )
    await db.execute(
        "UPDATE aios.timeline SET user_name='other' WHERE session_id=$1",
        origin.session_id,
    )
    with pytest.raises(HTTPException):
        await EnvironmentStore(db).create(
            bid, LinkIn(origin_instance_id=origin.instance_id, share_memory=True)
        )
    await db.execute(
        "UPDATE aios.timeline SET user_name='human' WHERE session_id=$1",
        origin.session_id,
    )
    await db.execute(
        "INSERT INTO aios.character_epistemic_profile VALUES('Cortana','isolated')"
    )
    with pytest.raises(HTTPException):
        await EnvironmentStore(db).create(
            bid, LinkIn(origin_instance_id=origin.instance_id, share_memory=True)
        )
    assert await scope(db, execution.instance_id) == {execution.instance_id}


@pytest.mark.asyncio
async def test_peer_knowledge_revisions_invalidate_without_chat_advancement(db):
    _, origin, execution, bid, link = await pair(db)
    before = await db.fetchval(
        "SELECT aios.cognitive_context_revision($1)", origin.instance_id
    )
    head = await db.fetchval(
        "SELECT source_head_node_id FROM aios.character_runtime_state WHERE instance_id=$1",
        origin.instance_id,
    )
    acquisition = await db.fetchval(
        "INSERT INTO aios.knowledge_acquisition_event(instance_id) VALUES($1) RETURNING acquisition_id",
        execution.instance_id,
    )
    changed = await db.fetchval(
        "SELECT aios.cognitive_context_revision($1)", origin.instance_id
    )
    assert before != changed
    assert head == await db.fetchval(
        "SELECT source_head_node_id FROM aios.character_runtime_state WHERE instance_id=$1",
        origin.instance_id,
    )
    await db.execute(
        "INSERT INTO aios.epistemic_authority_admission VALUES($1,'attested')",
        acquisition,
    )
    assert changed != await db.fetchval(
        "SELECT aios.cognitive_context_revision($1)", origin.instance_id
    )
    await EnvironmentStore(db).revoke(bid, link)
    assert await scope(db, origin.instance_id) == {origin.instance_id}


@pytest.mark.asyncio
async def test_delegation_survives_chat_turns_and_v2_cannot_claim(db):
    store, origin, _execution, bid, link = await pair(db)
    req = await request(db, origin)
    run = await EnvironmentStore(db).delegate(bid, link, req)
    assert run["origin_instance_id"] == str(origin.instance_id)
    assert run["required_protocol_version"] == 3
    assert await store.claim(bid, "worker") is None
    claimed = await store.claim(bid, "worker", 3)
    rid = UUID(claimed["run_id"])
    lease = LeaseIn(worker_key="worker", claim_token=claimed["claim_token"])
    next_node = uuid4()
    timeline = await db.fetchval(
        "SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1",
        origin.instance_id,
    )
    await db.execute("INSERT INTO aios.dag_node VALUES($1,$2,2)", next_node, timeline)
    await db.execute(
        "INSERT INTO aios.dag_edge VALUES($1,$2,$3)",
        timeline,
        next_node,
        req.source_node_id,
    )
    await db.execute(
        "UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1",
        origin.instance_id,
        next_node,
    )
    assert (await store.renew(bid, rid, lease))["ok"]
    replay = await EnvironmentStore(db).delegate(bid, link, req)
    assert replay["run_id"] == run["run_id"]
    with pytest.raises(HTTPException):
        await EnvironmentStore(db).delegate(
            bid, link, req.model_copy(update={"objective": "different"})
        )
    prog = ProgressIn(
        **lease.model_dump(),
        event_key=uuid4(),
        kind="progress",
        summary="Inspecting service permissions",
    )
    first = await EnvironmentStore(db).progress(bid, rid, prog)
    assert first == await EnvironmentStore(db).progress(bid, rid, prog)
    with pytest.raises(HTTPException):
        await EnvironmentStore(db).progress(
            bid, rid, prog.model_copy(update={"summary": "changed"})
        )
    timeline = await db.fetchval(
        "SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1",
        origin.instance_id,
    )
    overlay = await task_overlay(db, origin.instance_id, timeline)
    assert overlay[0]["summary"] == prog.summary
    assert "Inspecting service permissions" in render_hud_text(
        {"remote_tasks": overlay}
    )
    assert await task_overlay(db, origin.instance_id, uuid4()) == []
    ended = await store.finish(
        bid,
        rid,
        FinishIn(
            **lease.model_dump(), status="failed", result={"error": "permission denied"}
        ),
    )
    assert ended["status"] == "failed"
    assert (await task_overlay(db, origin.instance_id, timeline))[0][
        "status"
    ] == "failed"


@pytest.mark.asyncio
async def test_cancel_and_revocation_stop_new_work_without_replay(db):
    store, origin, _execution, bid, link = await pair(db)
    req = await request(db, origin)
    run = await EnvironmentStore(db).delegate(bid, link, req)
    rid = UUID(run["run_id"])
    claimed = await store.claim(bid, "worker", 3)
    lease = LeaseIn(worker_key="worker", claim_token=claimed["claim_token"])
    await EnvironmentStore(db).revoke(bid, link)
    assert (await store.renew(bid, rid, lease))["cancel_requested"] is True
    with pytest.raises(HTTPException):
        await EnvironmentStore(db).progress(
            bid,
            rid,
            ProgressIn(
                **lease.model_dump(),
                event_key=uuid4(),
                kind="progress",
                summary="still working",
            ),
        )
    with pytest.raises(HTTPException):
        await EnvironmentStore(db).recall(
            bid,
            rid,
            RecallIn(
                worker_key="worker",
                claim_token=lease.claim_token,
                query="Previous attempt?",
            ),
        )
    ended = await store.finish(
        bid, rid, FinishIn(**lease.model_dump(), status="finished")
    )
    assert ended["status"] == "cancelled"
    assert await store.claim(bid, "worker", 3) is None


@pytest.mark.asyncio
async def test_queued_cancel_is_terminal_and_not_claimed(db):
    store, origin, _execution, bid, link = await pair(db)
    run = await EnvironmentStore(db).delegate(bid, link, await request(db, origin))
    cancelled = await EnvironmentStore(db).cancel(bid, UUID(run["run_id"]))
    assert cancelled["status"] == "cancelled"
    assert await store.claim(bid, "worker", 3) is None


@pytest.mark.asyncio
async def test_goal_owner_stays_in_chat_and_closed_attempt_rejects_review(
    db, monkeypatch
):
    from aios_app.epistemic.goals import CharacterGoalService

    store, origin, _execution, bid, link = await pair(db)
    goal, attempt = uuid4(), uuid4()
    timeline = await db.fetchval(
        "SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1",
        origin.instance_id,
    )
    await db.execute(
        "INSERT INTO aios.character_agent_goal(goal_id,instance_id,status,goal_text) VALUES($1,$2,'active','Fix service')",
        goal,
        origin.instance_id,
    )
    await db.execute(
        "INSERT INTO aios.character_goal_attempt VALUES($1,$2,$3,$4,NULL)",
        attempt,
        goal,
        origin.instance_id,
        timeline,
    )
    req = await request(
        db,
        origin,
        goal_id=goal,
        attempt_id=attempt,
        required_checks=[{"id": "unit", "command": "python -m unittest"}],
    )
    run = await EnvironmentStore(db).delegate(bid, link, req)
    claimed = await store.claim(bid, "worker", 3)
    rid = UUID(run["run_id"])
    lease = LeaseIn(worker_key="worker", claim_token=claimed["claim_token"])
    await store.finish(
        bid,
        rid,
        FinishIn(
            **lease.model_dump(),
            status="finished",
            result={"evidence": valid_evidence(req.spec())},
        ),
    )
    finish = AsyncMock()
    monkeypatch.setattr(CharacterGoalService, "finish", finish)
    review = ReviewIn(accept=True, reviewer="operator", reason="Inspected evidence")
    await store.review(bid, rid, review)
    assert finish.call_args.kwargs["instance_id"] == origin.instance_id
    assert finish.call_args.kwargs["attempt_id"] == attempt
    assert finish.call_args.kwargs["source_node_id"] == req.source_node_id
    # A different run's late evidence cannot close an ended attempt.
    later = await EnvironmentStore(db).delegate(
        bid, link, req.model_copy(update={"request_id": uuid4()})
    )
    claimed = await store.claim(bid, "worker", 3)
    rid2 = UUID(later["run_id"])
    await store.finish(
        bid,
        rid2,
        FinishIn(
            worker_key="worker",
            claim_token=claimed["claim_token"],
            status="finished",
            result={"evidence": valid_evidence(req.spec())},
        ),
    )
    await db.execute(
        "UPDATE aios.character_goal_attempt SET closed_at=now() WHERE attempt_id=$1",
        attempt,
    )
    with pytest.raises(HTTPException):
        await store.review(bid, rid2, review)
    assert finish.await_count == 1


@pytest.mark.asyncio
async def test_recall_uses_binding_identity_and_rechecks_revocation(db, monkeypatch):
    from aios_app.epistemic.inquiry.contracts import InquiryEvidence, InquiryHit
    from aios_app.epistemic.inquiry.lookup import InquiryLookup

    store, origin, execution, bid, link = await pair(db)
    run = await EnvironmentStore(db).delegate(bid, link, await request(db, origin))
    claim = await store.claim(bid, "worker", 3)
    rid = UUID(run["run_id"])
    req = RecallIn(
        worker_key="worker",
        claim_token=claim["claim_token"],
        query="What did I try before?",
    )

    async def search(demand, **kwargs):
        assert demand.instance_id == execution.instance_id
        assert kwargs["target"] == "personal"
        return InquiryEvidence(
            "partial",
            (
                InquiryHit(
                    "char",
                    "e1",
                    "I tried changing the service account",
                    {"source_node_id": "n1"},
                    True,
                ),
            ),
        )

    # Patch as a staticmethod so this records only the intended demand.
    monkeypatch.setattr(InquiryLookup, "search", staticmethod(search))
    result = await EnvironmentStore(db).recall(bid, rid, req)
    assert result["hits"][0]["durable_knowledge"] is True

    async def changed(demand, **kwargs):
        await EnvironmentStore(db).revoke(bid, link)
        return InquiryEvidence("partial", (InquiryHit("char", "e1", "private"),))

    monkeypatch.setattr(InquiryLookup, "search", staticmethod(changed))
    with pytest.raises(HTTPException):
        await EnvironmentStore(db).recall(bid, rid, req)


def test_link_management_requires_operator_credential(monkeypatch):
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    monkeypatch.setenv("AIOS_WORKSPACE_API_KEY", "worker")
    monkeypatch.setenv("AIOS_WORKSPACE_REVIEW_KEY", "operator")
    with TestClient(app) as c:
        r = c.post(
            f"/workspace-client/v3/{uuid4()}/links",
            headers={"Authorization": "Bearer worker"},
            json={"origin_instance_id": str(uuid4()), "share_memory": True},
        )
        assert r.status_code == 401
        p = c.get(
            "/workspace-client/protocol", headers={"Authorization": "Bearer worker"}
        ).json()
        assert p["supported_versions"] == [2, 3]


@pytest.mark.asyncio
async def test_source_branch_replacement_stops_a_delegated_run(db):
    store, origin, _execution, bid, link = await pair(db)
    req = await request(db, origin)
    run = await EnvironmentStore(db).delegate(bid, link, req)
    claim = await store.claim(bid, "worker", 3)
    unrelated_head = uuid4()
    timeline = await db.fetchval(
        "SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1",
        origin.instance_id,
    )
    await db.execute(
        "INSERT INTO aios.dag_node VALUES($1,$2,9)", unrelated_head, timeline
    )
    await db.execute(
        "UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1",
        origin.instance_id,
        unrelated_head,
    )
    with pytest.raises(HTTPException, match="active conversation branch"):
        await store.renew(
            bid,
            UUID(run["run_id"]),
            LeaseIn(worker_key="worker", claim_token=claim["claim_token"]),
        )


@pytest.mark.asyncio
async def test_revocation_reconciles_old_materialized_winners(db, monkeypatch):
    from aios_app.epistemic import belief_reconciliation

    _, origin, _execution, bid, link = await pair(db)
    atom = uuid4()
    await db.execute(
        "INSERT INTO aios.character_belief_state VALUES($1,$2)",
        origin.instance_id,
        atom,
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(
        belief_reconciliation, "reconcile_character_belief_atom", reconcile
    )
    await EnvironmentStore(db).revoke(bid, link)
    reconcile.assert_awaited_once_with(db, instance_id=origin.instance_id, atom_id=atom)
