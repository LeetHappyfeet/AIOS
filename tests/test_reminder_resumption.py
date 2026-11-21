"""Real PostgreSQL transitions with model responses supplied by the test."""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from aios_app.agent.goal_planning import validate_plan
from aios_app.agent.reminder_continuation import ReminderContinuation, report_revision, request_id
from aios_app.agent.temporal import TemporalTriggerStore
from aios_app.agent.work_admission import WorkRequestIn
from aios_app.agent.work_evidence import WorkEvidence
from aios_app.agent.workspaces import FinishIn, WorkspaceStore
from test_work_admission import setup
from test_workspace_client import db

@pytest_asyncio.fixture
async def project(db, monkeypatch):
    monkeypatch.setattr("aios_app.agent.lifecycle.CharacterAgencyStore._refresh_scene", AsyncMock())
    # Match asyncpg's native timestamp values at this isolated engine boundary.
    connection = db.pool.con
    original_fetch = connection.fetch
    async def fetch_dates(sql, *args):
        rows = await original_fetch(sql, *args)
        for row in rows:
            for key, value in row.items():
                if isinstance(value, str) and (key.endswith("_at") or key.endswith("_until")):
                    row[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return rows
    monkeypatch.setattr(connection, "fetch", fetch_dates)
    work, origin, execution, bid, lid, source, req, profile = await setup(db, online=False)
    await db.execute("ALTER TABLE aios.character_agent_runtime ADD COLUMN cooldown_until timestamptz")
    await db.execute(
        "INSERT INTO aios.character_agent_runtime(instance_id) VALUES($1) ON CONFLICT DO NOTHING",
        origin.instance_id,
    )
    await db.execute("""CREATE TABLE aios.instance_goal_activity(
        instance_id uuid PRIMARY KEY,explicit_work_until timestamptz)""")
    await db.execute("""CREATE TABLE aios.character_goal_plan(
        plan_id uuid PRIMARY KEY,instance_id uuid,goal_id uuid,attempt_id uuid,
        status text,proposal jsonb,revision integer,input_fingerprint text)""")
    await db.execute("""CREATE TABLE aios.pipeline_job(job_type text,payload jsonb,status text)""")
    await db.execute("""CREATE TABLE aios.character_cognitive_operation(
        operation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),instance_id uuid,
        status text,result jsonb,input jsonb,created_at timestamptz DEFAULT now())""")
    await db.execute("""ALTER TABLE aios.character_wake_event
        ADD COLUMN status text DEFAULT 'pending'""")
    trigger, goal, attempt, plan = (uuid4() for _ in range(4))
    timeline = await db.fetchval(
        "SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1", origin.instance_id,
    )
    await db.execute("""INSERT INTO aios.character_agent_goal(goal_id,instance_id,status,goal_text)
        VALUES($1,$2,'active','Research glaciation')""", goal, origin.instance_id)
    await db.execute("""INSERT INTO aios.character_goal_attempt(attempt_id,goal_id,instance_id,source_timeline_id)
        VALUES($1,$2,$3,$4)""", attempt, goal, origin.instance_id, timeline)
    await db.execute("""INSERT INTO aios.character_temporal_trigger(trigger_id,instance_id,status,due_at,last_outcome,payload)
        VALUES($1,$2,'completed',now(),'review',$3::jsonb)""", trigger, origin.instance_id, json.dumps({
            "source_timeline_id": str(timeline), "reminder_goal_id": str(goal), "attempt_id": str(attempt),
        }))
    await db.execute("""INSERT INTO aios.character_reminder_continuation(
        trigger_id,instance_id,goal_id,attempt_id,source_timeline_id)
        VALUES($1,$2,$3,$4,$5)""", trigger, origin.instance_id, goal, attempt, timeline)
    ctx = SimpleNamespace(source_timeline_id=timeline, source_head_node_id=source, state_version=1)
    snapshot = {
        "goal_text": "Research glaciation", "context": ctx, "fingerprint": "initial",
        "attempt_id": attempt, "work_receipts": [],
    }
    snapshot_fn = AsyncMock(return_value=snapshot)
    monkeypatch.setattr("aios_app.agent.goal_planning.GoalPlanningService.snapshot", snapshot_fn)
    await db.execute("""INSERT INTO aios.character_goal_plan VALUES($1,$2,$3,$4,'ready',$5::jsonb,1,'initial')""",
        plan, origin.instance_id, goal, attempt, json.dumps({"next": "workspace_work", "open_questions": []}))
    async def hud(*args, **kwargs):
        return SimpleNamespace(
            prompt="I am Renamon. I investigate carefully.",
            source_timeline_id=timeline, source_node_id=source, state_version=1,
        )
    monkeypatch.setattr("aios_app.agent.reminder_continuation.InternalHUDAssembler.build", hud)
    infer = AsyncMock(return_value=SimpleNamespace(response=SimpleNamespace(raw={
        "decision": "request_work", "capability": req.capability, "purpose": req.purpose,
        "objective": req.objective,
    })))
    monkeypatch.setattr("aios_app.agent.reminder_continuation.InferenceBroker.infer", infer)
    return SimpleNamespace(db=db, service=ReminderContinuation(db), trigger=trigger, goal=goal,
        attempt=attempt, plan=plan, instance=origin.instance_id, bid=bid, source=source, timeline=timeline,
        snapshot=snapshot, snapshot_fn=snapshot_fn, infer=infer, work=work)


async def state(project):
    return await project.db.fetchrow(
        "SELECT * FROM aios.character_reminder_continuation WHERE trigger_id=$1", project.trigger,
    )


@pytest.mark.asyncio
async def test_wait_plan_blocks_executable_decision_and_waiting_goal_keeps_its_turn(project):
    p = project
    await p.db.execute("""UPDATE aios.character_goal_plan SET proposal='{"next":"wait","open_questions":[]}'""")
    await p.service.run(p.trigger)
    row = await state(p)
    assert row["status"] == "waiting_evidence"
    p.infer.assert_not_awaited()
    await p.service.run(p.trigger)
    p.infer.assert_not_awaited()
    assert (await state(p))["unchanged_checks"] > row["unchanged_checks"]


@pytest.mark.asyncio
async def test_offline_request_survives_restart_and_report_is_reviewed_once(project):
    p = project
    await p.service.run(p.trigger)
    row = await state(p)
    assert row["status"] == "waiting_work"
    assert p.infer.await_count == 1
    assert "I am Renamon" in p.infer.call_args.args[0].prompt
    req = await p.db.fetchrow("SELECT * FROM aios.character_work_request WHERE request_id=$1", row["work_request_id"])
    assert req["status"] == "waiting_client"
    # A fresh service object resumes the exact persisted request.
    restarted = ReminderContinuation(p.db)
    await restarted.run(p.trigger)
    assert await p.db.fetchval("SELECT count(*) FROM aios.character_work_request") == 1
    p.infer.assert_awaited_once()

    await p.db.execute("UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1", p.bid)
    await restarted.run(p.trigger)
    assert await p.db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1
    run = await WorkspaceStore(p.db).claim(p.bid, "worker", 3)
    assert run
    # Terminal worker evidence is kept as a report, with no goal completion.
    result = {"result_summary": {"summary": "Located glaciation evidence."},
              "findings": ["The worker located a relevant chapter."], "limitations": ["Chapter not fully read."]}
    await p.db.execute("""UPDATE aios.character_reminder_continuation
        SET next_evaluation_at=now()+interval '15 minutes' WHERE trigger_id=$1""", p.trigger)
    finish = FinishIn(worker_key="worker", claim_token=run["claim_token"], status="failed", result=result)
    await WorkspaceStore(p.db).finish(p.bid, UUID(run["run_id"]), finish)
    await WorkspaceStore(p.db).finish(p.bid, UUID(run["run_id"]), finish)
    assert await p.db.fetchval("""SELECT next_evaluation_at<=now()
        FROM aios.character_reminder_continuation WHERE trigger_id=$1""", p.trigger)
    await restarted.run(p.trigger)
    row = await state(p)
    assert row["status"] == "awaiting_review"
    reports = await WorkEvidence(p.db).planning(p.instance, p.goal, p.attempt)
    assert row["receipt_revision"] == report_revision(reports)
    assert await p.db.fetchval("SELECT count(*) FROM aios.character_wake_event WHERE source_type='reminder_review'") == 1
    await restarted.run(p.trigger)
    assert await p.db.fetchval("SELECT count(*) FROM aios.character_wake_event WHERE source_type='reminder_review'") == 1

    operation = {"instance_id": p.instance, "source_timeline_id": p.timeline, "input": {
        "reminder_trigger_id": str(p.trigger), "reminder_receipt_revision": row["receipt_revision"],
    }}
    await restarted.record(operation, p.goal, p.attempt)
    await restarted.record(operation, p.goal, p.attempt)
    acknowledged = await state(p)
    assert acknowledged["status"] == "pending"
    assert acknowledged["generation"] == 1
    assert acknowledged["reviewed_receipt_revision"] == row["receipt_revision"]
    assert acknowledged["work_request_id"] is None
    assert await p.db.fetchval("SELECT status FROM aios.character_agent_goal WHERE goal_id=$1", p.goal) == "active"
    assert request_id(p.trigger, 1) != request_id(p.trigger)

    p.snapshot["fingerprint"] = "report-arrived"
    p.snapshot["work_receipts"] = reports
    p.snapshot_fn.return_value = p.snapshot
    await p.db.execute("""UPDATE aios.character_goal_plan SET input_fingerprint='report-arrived',
                         proposal='{"next":"wait","open_questions":[]}'""")
    await restarted.run(p.trigger)
    assert (await state(p))["status"] == "waiting_evidence"
    assert await p.db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1


@pytest.mark.asyncio
async def test_invalid_proposals_have_bounded_recovery_without_spending_budget_on_outages(project):
    p = project
    from aios_app.inference import InferenceUnavailable
    p.infer.return_value.response.raw = {"decision": "invented"}
    await p.service.run(p.trigger)
    assert (await state(p))["invalid_decisions"] == 1
    p.infer.side_effect = InferenceUnavailable("offline")
    await p.service.run(p.trigger)
    assert (await state(p))["invalid_decisions"] == 1
    p.infer.side_effect = None
    await p.service.run(p.trigger)
    assert (await state(p))["invalid_decisions"] == 2
    await p.service.run(p.trigger)
    row = await state(p)
    assert row["status"] == "failed"
    assert row["invalid_decisions"] == 3
    await p.service.run(p.trigger)
    assert p.infer.await_count == 4
    assert await p.db.fetchval("SELECT count(*) FROM aios.character_work_request") == 0


@pytest.mark.asyncio
async def test_suspended_timer_can_be_cancelled_and_closed_attempt_never_requests_work(project):
    p = project
    await p.db.execute("UPDATE aios.character_temporal_trigger SET status='timeline_inactive' WHERE trigger_id=$1", p.trigger)
    cancelled = await TemporalTriggerStore(p.db).cancel(instance_id=p.instance, trigger_id=p.trigger)
    assert cancelled["status"] == "cancelled"
    await p.db.execute("UPDATE aios.character_goal_attempt SET closed_at=now() WHERE attempt_id=$1", p.attempt)
    await p.service.run(p.trigger)
    assert (await state(p))["status"] == "closed"
    p.infer.assert_not_awaited()


def test_workspace_plan_requires_resolved_prerequisites():
    from test_goal_planning import proposal
    raw = proposal(next_step="workspace_work")
    with pytest.raises(ValueError, match="prerequisites"):
        validate_plan(raw, {"goal"})
    raw["open_questions"] = []
    assert validate_plan(raw, {"goal"})["next"] == "workspace_work"


@pytest.mark.asyncio
async def test_waiting_project_does_not_monopolize_instance_admission(project, monkeypatch):
    p = project
    row = await state(p)
    await p.service._park(row, "waiting_information", "Waiting for a prerequisite.", "unchanged")
    second = uuid4()
    await p.db.execute("""INSERT INTO aios.character_temporal_trigger(
        trigger_id,instance_id,due_at) VALUES($1,$2,now())""", second, p.instance)
    await p.db.execute("""INSERT INTO aios.character_reminder_continuation(
        trigger_id,instance_id,goal_id,attempt_id,source_timeline_id)
        VALUES($1,$2,$3,$4,$5)""", second, p.instance, p.goal, p.attempt, p.timeline)
    async def enqueue(db, **kwargs):
        await db.execute("INSERT INTO aios.pipeline_job VALUES($1,$2::jsonb,'queued')",
                         kwargs["job_type"], json.dumps(kwargs["payload"]))
        return uuid4()
    monkeypatch.setattr("aios_app.agent.reminder_continuation.enqueue_job", enqueue)
    await p.service.enqueue_due()
    payload = await p.db.fetchval("SELECT payload FROM aios.pipeline_job")
    assert payload["trigger_id"] == str(second)


@pytest.mark.asyncio
async def test_changed_source_discards_only_draft_and_replans_original_attempt(project, monkeypatch):
    p = project
    from fastapi import HTTPException
    submit = AsyncMock(side_effect=HTTPException(409, "Reminder plan or original goal attempt changed"))
    monkeypatch.setattr("aios_app.agent.work_admission.WorkAdmission.request", submit)
    await p.service.run(p.trigger)
    row = await state(p)
    assert row["status"] == "pending"
    assert row["generation"] == 1
    assert row["intent"] is None
    assert row["attempt_id"] == p.attempt
    assert await p.db.fetchval("SELECT count(*) FROM aios.character_work_request") == 0
    other_timeline = uuid4()
    await p.db.execute("UPDATE aios.character_runtime_state SET source_timeline_id=$1 WHERE instance_id=$2",
                       other_timeline, p.instance)
    await p.service.run(p.trigger)
    p.infer.assert_awaited_once()
    assert (await state(p))["status"] == "pending"


@pytest.mark.asyncio
async def test_report_review_recovers_failed_operation_with_bounded_attempts(project):
    p = project
    row = await state(p)
    await p.service._queue_review(row, "report-revision")
    row = await state(p)
    assert row["review_attempt"] == 1
    await p.db.execute("UPDATE aios.character_wake_event SET status='consumed'")
    await p.service._review_status(row, p.snapshot)
    row = await state(p)
    assert row["review_attempt"] == 2
    await p.db.execute("UPDATE aios.character_wake_event SET status='consumed'")
    await p.service._review_status(row, p.snapshot)
    row = await state(p)
    assert row["review_attempt"] == 3
    await p.db.execute("UPDATE aios.character_wake_event SET status='consumed'")
    await p.service._review_status(row, p.snapshot)
    assert (await state(p))["status"] == "failed"
