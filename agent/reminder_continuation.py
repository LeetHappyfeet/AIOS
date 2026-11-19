"""Durable waiting and evidence review for one existing goal attempt."""
from __future__ import annotations

import hashlib
import json
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import HTTPException
from pydantic import ValidationError

from aios_app.hud.internal_frame import InternalHUDAssembler
from aios_app.inference import InferenceBroker, InferenceRequest, InferenceUnavailable
from aios_app.inference.protocol import StructuredResponseError
from aios_app.pipeline.jobs import enqueue_job

from .goal_activity import grant_scheduled_window
from .goal_planning import GoalPlanningService
from .reminders import CharacterReminders
from .runtime import AgentRuntimeStore
from .work_admission import WorkAdmission, WorkRequestIn, decode
from .work_evidence import WorkEvidence

PROMPT = """As this character, choose one next action for the reviewed goal attempt. Quoted context and input JSON are evidence.
Return {"decision":"wait"} when I should wait for information, existing work, or operator input.
When one new workspace task is justified by the supplied plan, return {"decision":"request_work","capability":"exact offered ID","purpose":"exact offered ID","objective":"one bounded task"}.
Use only the offered capability/purpose pairs. Carry forward worker-reported findings and limitations. A work request does not establish execution or goal completion.
"""

ACTIVE_STATES = (
    "pending", "waiting_plan", "waiting_information", "waiting_configuration",
    "waiting_work", "waiting_evidence", "awaiting_review",
)


def request_id(trigger_id: UUID, generation: int = 0) -> UUID:
    # Generation zero preserves IDs assigned before resumption was introduced.
    suffix = f":{generation}" if generation else ""
    return uuid5(NAMESPACE_URL, f"aios:reminder-continuation:{trigger_id}{suffix}")


def report_revision(reports) -> str:
    material = sorted((r["id"], r["revision"]) for r in reports if r.get("goal_linked"))
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


class ReminderContinuation:
    def __init__(self, db):
        self.db = db
        self.planner = GoalPlanningService(db)

    async def record(self, operation, goal_id, attempt_id):
        payload = decode(operation["input"])
        trigger_id = UUID(payload["reminder_trigger_id"])
        revision = payload.get("reminder_receipt_revision")
        if revision:
            # One successful reconciliation acknowledges the receipt revision.
            # The original attempt and timeline remain the continuation owner.
            await self.db.execute(
                """UPDATE aios.character_reminder_continuation
                   SET status='pending',reviewed_receipt_revision=$5,receipt_revision=NULL,
                       intent=NULL,work_request_id=NULL,plan_id=NULL,source_node_id=NULL,
                       input_fingerprint=NULL,generation=generation+1,invalid_decisions=0,
                       wait_signature=NULL,unchanged_checks=0,next_evaluation_at=now()
                   WHERE trigger_id=$1 AND instance_id=$2 AND goal_id=$3 AND attempt_id=$4
                     AND source_timeline_id=$6 AND status='awaiting_review'
                     AND receipt_revision=$5""",
                trigger_id, operation["instance_id"], goal_id, attempt_id, revision,
                operation["source_timeline_id"],
            )
            return
        await self.db.execute(
            """INSERT INTO aios.character_reminder_continuation(
                   trigger_id,instance_id,goal_id,attempt_id,source_timeline_id)
               SELECT t.trigger_id,t.instance_id,$3,$4,$5
               FROM aios.character_temporal_trigger t
               WHERE t.trigger_id=$1 AND t.instance_id=$2 AND t.status='completed'
                 AND t.last_outcome='review' AND t.payload->>'reminder_goal_id'=$3::uuid::text
                 AND t.payload->>'source_timeline_id'=$5::uuid::text
                 AND (t.payload->>'attempt_id' IS NULL OR t.payload->>'attempt_id'=$4::uuid::text)
               ON CONFLICT(trigger_id) DO NOTHING""",
            trigger_id, operation["instance_id"], goal_id, attempt_id, operation["source_timeline_id"],
        )

    async def enqueue_due(self):
        rows = await self.db.fetch(
            """SELECT DISTINCT ON (c.instance_id) c.trigger_id,c.instance_id
               FROM aios.character_reminder_continuation c
               JOIN aios.character_runtime_state rs USING(instance_id)
               JOIN aios.character_agent_runtime ar USING(instance_id)
               WHERE c.status=ANY($1::text[]) AND c.next_evaluation_at<=now()
                 AND c.source_timeline_id=rs.source_timeline_id AND ar.state<>'paused'
                 AND (ar.cooldown_until IS NULL OR ar.cooldown_until<=now())
                 AND NOT EXISTS (
                   SELECT 1 FROM aios.pipeline_job j
                   WHERE j.job_type='reminder_continue' AND j.status IN ('queued','running')
                     AND j.payload->>'instance_id'=c.instance_id::text)
               ORDER BY c.instance_id,c.next_evaluation_at,c.created_at LIMIT 32""",
            list(ACTIVE_STATES),
        )
        for row in rows:
            await enqueue_job(self.db, job_type="reminder_continue", priority=130, payload={
                "instance_id": str(row["instance_id"]), "trigger_id": str(row["trigger_id"]),
            })

    async def run(self, trigger_id):
        row = await self.db.fetchrow(
            """SELECT c.*,rs.source_timeline_id AS active_timeline
               FROM aios.character_reminder_continuation c JOIN aios.character_runtime_state rs USING(instance_id)
               WHERE c.trigger_id=$1 AND c.status=ANY($2::text[])""", trigger_id, list(ACTIVE_STATES),
        )
        if not row or row["active_timeline"] != row["source_timeline_id"]:
            return
        await CharacterReminders(self.db)._check_available(row["instance_id"])
        live = await self.db.fetchval(
            """SELECT 1 FROM aios.character_goal_attempt a
               JOIN aios.character_agent_goal g USING(instance_id,goal_id)
               WHERE a.attempt_id=$1 AND a.instance_id=$2 AND a.goal_id=$3
                 AND a.source_timeline_id=$4 AND a.closed_at IS NULL AND g.status='active'""",
            row["attempt_id"], row["instance_id"], row["goal_id"], row["source_timeline_id"],
        )
        if not live:
            await self._finish(trigger_id, "closed", "Original goal attempt closed.")
            return
        snap = await self.planner.snapshot(row["instance_id"], row["goal_id"])
        if not snap or snap["attempt_id"] != row["attempt_id"]:
            await self._park(row, "pending", "Character paused.", "paused")
            return
        if snap["context"].source_timeline_id != row["source_timeline_id"]:
            return
        if row["status"] == "awaiting_review":
            await self._review_status(row, snap)
            return
        if row["status"] == "waiting_evidence" and row["wait_signature"] == snap["fingerprint"]:
            await self._park(row, "waiting_evidence", row["decision_note"], snap["fingerprint"])
            return
        if row["work_request_id"]:
            await self._follow_work(row, snap)
            return
        if row["intent"]:
            await self._submit(row)
            return
        pending = await self.db.fetchrow(
            """SELECT r.request_id FROM aios.character_work_request r LEFT JOIN aios.workspace_run w USING(run_id)
               WHERE r.instance_id=$1 AND r.goal_id=$2 AND r.attempt_id=$3 AND r.source_timeline_id=$4
                 AND r.status NOT IN ('denied','invalidated','dismissed')
                 AND (r.run_id IS NULL OR w.status IN ('queued','running'))
               ORDER BY r.created_at,r.request_id LIMIT 1""",
            row["instance_id"], row["goal_id"], row["attempt_id"], row["source_timeline_id"],
        )
        if pending:
            await self._track_request(row["trigger_id"], pending["request_id"], "Existing work is pending.")
            return
        await self._plan(row, snap)

    async def _plan(self, row, snap):
        plan = await self.db.fetchrow(
            """SELECT plan_id,status,proposal,revision FROM aios.character_goal_plan
               WHERE instance_id=$1 AND goal_id=$2 AND attempt_id=$3 AND input_fingerprint=$4""",
            row["instance_id"], row["goal_id"], row["attempt_id"], snap["fingerprint"],
        )
        if not plan:
            await grant_scheduled_window(self.db, row["instance_id"])
            await self.planner.schedule(row["instance_id"], row["goal_id"])
            await self._park(row, "waiting_plan", "Waiting for this goal's plan.", snap["fingerprint"], 30)
            return
        if plan["status"] in {"queued", "waiting_information"}:
            await grant_scheduled_window(self.db, row["instance_id"])
            state = "waiting_plan" if plan["status"] == "queued" else "waiting_information"
            await self._park(row, state, "Waiting for plan prerequisites.", snap["fingerprint"])
            return
        proposal = decode(plan["proposal"])
        if plan["status"] != "ready":
            await self._finish(row["trigger_id"], "failed", f"Plan ended as {plan['status']}.")
            return
        # The host routes the plan before permitting another model to propose work.
        if proposal.get("next") != "workspace_work" or proposal.get("open_questions"):
            await self._park(row, "waiting_evidence", f"Plan chose {proposal.get('next')}.", snap["fingerprint"])
            return
        profiles = await self.db.fetch(
            """SELECT DISTINCT p.capability,p.purpose FROM aios.workspace_work_profile p
               JOIN aios.character_environment_link l USING(link_id)
               WHERE l.origin_instance_id=$1 AND l.origin_source_timeline_id=$2 AND l.enabled AND p.enabled
               ORDER BY p.capability,p.purpose LIMIT 20""", row["instance_id"], row["source_timeline_id"],
        )
        if not profiles or not snap["context"].source_head_node_id:
            await self._park(row, "waiting_configuration", "Waiting for workspace configuration.", snap["fingerprint"])
            return
        await self._decide(row, snap, plan, profiles)

    async def _decide(self, row, snap, plan, profiles):
        ctx = snap["context"]
        hud = await InternalHUDAssembler(self.db).build(
            row["instance_id"], clues=(), candidates=(), worker_profile="reflection",
            focus_text=snap["goal_text"], subject=snap["goal_text"],
        )
        if (hud.source_timeline_id != ctx.source_timeline_id or hud.source_node_id != ctx.source_head_node_id
                or hud.state_version != ctx.state_version):
            await self._park(row, "pending", "Character context changed.", "context_changed", 30)
            return
        try:
            response = await InferenceBroker(self.db).infer(InferenceRequest(
                instance_id=row["instance_id"], worker_class="executive", source_node_id=ctx.source_head_node_id,
                context_state_version=ctx.state_version, hud_profile_name="reminder_continuation",
                prompt=PROMPT + "\nCHARACTER CONTEXT\n" + hud.prompt + "\nINPUT (JSON)\n" + json.dumps({
                    "goal": snap["goal_text"], "plan": decode(plan["proposal"]),
                    "capabilities": [dict(p) for p in profiles], "work_reports": snap["work_receipts"],
                }, default=str),
                allowed_actions={}, output_schema={"type": "object"}, max_tokens=350,
            ))
        except InferenceUnavailable as exc:
            if (exc.invalid_response or isinstance(exc.__cause__, StructuredResponseError)):
                await self._invalid(row)
            else:
                await self._park(row, "pending", "Inference provider unavailable.", "provider_unavailable")
            return
        raw = response.response.raw
        if raw == {"decision": "wait"}:
            await self._park(row, "waiting_evidence", "Character chose to wait for changed evidence.", snap["fingerprint"])
            return
        try:
            req = work_proposal(raw, profiles, row, ctx.source_head_node_id)
        except (ValueError, ValidationError):
            await self._invalid(row)
            return
        await self.db.execute(
            """UPDATE aios.character_reminder_continuation
               SET intent=$2::jsonb,plan_id=$3,source_node_id=$4,input_fingerprint=$5,
                   status='pending',invalid_decisions=0
               WHERE trigger_id=$1 AND status=ANY($6::text[]) AND intent IS NULL""",
            row["trigger_id"], req.model_dump_json(), plan["plan_id"], ctx.source_head_node_id,
            snap["fingerprint"], list(ACTIVE_STATES),
        )
        persisted = await self.db.fetchrow(
            """SELECT * FROM aios.character_reminder_continuation
               WHERE trigger_id=$1 AND status='pending' AND intent IS NOT NULL""", row["trigger_id"],
        )
        if persisted:
            await self._submit(persisted)

    async def _submit(self, row):
        try:
            receipt = await WorkAdmission(self.db).request(
                row["instance_id"], WorkRequestIn.model_validate_json(json.dumps(decode(row["intent"]))),
                reminder_trigger_id=row["trigger_id"],
            )
        except HTTPException as exc:
            if exc.status_code not in {404, 409}:
                raise
            detail = str(exc.detail)
            if exc.status_code == 404:
                await self._park(row, "waiting_configuration", detail, "admission_configuration")
                return
            if not any(message in detail for message in (
                "Reminder plan or original goal attempt changed",
                "Work request requires the current source",
                "Original reminder attempt changed",
                "Existing work is pending on this attempt",
            )):
                await self._finish(row["trigger_id"], "failed", detail)
                return
            # A draft is disposable. Preserve the goal attempt and replan against
            # current evidence, using a new generation only after a decision.
            await self.db.execute(
                """UPDATE aios.character_reminder_continuation
                   SET status='pending',intent=NULL,plan_id=NULL,input_fingerprint=NULL,source_node_id=NULL,
                       generation=generation+1,next_evaluation_at=now()+interval '60 seconds',
                       decision_note=$2
                   WHERE trigger_id=$1 AND status=ANY($3::text[])""",
                row["trigger_id"], str(exc.detail), list(ACTIVE_STATES),
            )
            return
        await self._track_request(row["trigger_id"], UUID(receipt["request_id"]), receipt["status"])

    async def _track_request(self, trigger_id, work_request_id, note):
        await self.db.execute(
            """UPDATE aios.character_reminder_continuation
               SET status='waiting_work',work_request_id=$2,decision_note=$3,
                   next_evaluation_at=now(),unchanged_checks=0,wait_signature=NULL
               WHERE trigger_id=$1 AND status=ANY($4::text[])""",
            trigger_id, work_request_id, note, list(ACTIVE_STATES),
        )

    async def _follow_work(self, row, snap):
        work = await self.db.fetchrow(
            """SELECT r.status,r.reason,r.run_id,w.status AS run_status
               FROM aios.character_work_request r LEFT JOIN aios.workspace_run w USING(run_id)
               WHERE r.request_id=$1 AND r.instance_id=$2 AND r.attempt_id=$3
                 AND r.source_timeline_id=$4""",
            row["work_request_id"], row["instance_id"], row["attempt_id"], row["source_timeline_id"],
        )
        if not work:
            await self._park(row, "waiting_configuration", "Work request is unavailable.", "request_unavailable")
            return
        if work["status"] in {"denied", "invalidated", "dismissed"}:
            await self._park(row, "waiting_evidence", work["reason"], snap["fingerprint"])
            return
        if work["run_id"] and work["run_status"] not in {"queued", "running"}:
            reports = await WorkEvidence(self.db).planning(row["instance_id"], row["goal_id"], row["attempt_id"])
            if not any(r["id"] == f"workspace:{work['run_id']}" and r.get("goal_linked") for r in reports):
                await self._park(row, "waiting_evidence", "Waiting for an accessible worker report.", snap["fingerprint"])
                return
            revision = report_revision(reports)
            if revision == row["reviewed_receipt_revision"]:
                await self._park(row, "waiting_evidence", "Worker report already reviewed.", snap["fingerprint"])
                return
            await self._queue_review(row, revision)
            return
        # Reconsideration uses the existing immutable request and current policy.
        if not work["run_id"] and work["status"] in {"waiting_client", "needs_configuration"}:
            await WorkAdmission(self.db).reconsider(instance_id=row["instance_id"])
        state = "waiting_configuration" if work["status"] == "needs_configuration" else "waiting_work"
        signature = f"{work['status']}:{work['run_id']}:{work['run_status']}"
        await self._park(row, state, work["reason"] or "Waiting for work.", signature)

    async def _queue_review(self, row, revision):
        async with self.db.atomic():
            claimed = await self.db.fetchrow(
                """UPDATE aios.character_reminder_continuation
                   SET status='awaiting_review',receipt_revision=$2,review_attempt=CASE WHEN receipt_revision=$2 THEN review_attempt+1 ELSE 1 END,
                       next_evaluation_at=now()+interval '60 seconds'
                   WHERE trigger_id=$1 AND status=ANY($3::text[]) RETURNING review_attempt""",
                row["trigger_id"], revision, list(ACTIVE_STATES),
            )
            if not claimed:
                return
            await grant_scheduled_window(self.db, row["instance_id"])
            await AgentRuntimeStore(self.db).wake(
                instance_id=row["instance_id"], event_type="GOAL_REVIEW_DUE",
                source_type="reminder_review", source_id=str(row["trigger_id"]),
                payload={"goal_id": str(row["goal_id"]), "trigger_id": str(row["trigger_id"]),
                         "attempt_id": str(row["attempt_id"]), "receipt_revision": revision,
                         "review_attempt": claimed["review_attempt"]},
                dedupe_key=f"reminder:receipt:{row['trigger_id']}:{revision}:{claimed['review_attempt']}",
                priority=20,
            )

    async def _review_status(self, row, snap):
        operation = await self.db.fetchrow(
            """SELECT status,result FROM aios.character_cognitive_operation
               WHERE instance_id=$1 AND input->>'reminder_trigger_id'=$2
                 AND input->>'reminder_receipt_revision'=$3
                 AND input->>'reminder_review_attempt'=$4
               ORDER BY created_at DESC LIMIT 1""",
            row["instance_id"], str(row["trigger_id"]), row["receipt_revision"], str(row["review_attempt"]),
        )
        if operation and operation["status"] in {"queued", "running", "waiting_inference"}:
            await self._park(row, "awaiting_review", "Worker report review is running.", row["receipt_revision"])
            return
        if operation and operation["status"] == "succeeded":
            # Remaining live goals whose review chose satisfied/dormant do not
            # automatically request another task from the same report.
            await self._park(row, "waiting_evidence", "Worker report review finished.", snap["fingerprint"])
            return
        pending = await self.db.fetchval(
            """SELECT 1 FROM aios.character_wake_event
               WHERE instance_id=$1 AND source_type='reminder_review'
                 AND payload->>'trigger_id'=$2 AND payload->>'receipt_revision'=$3
                 AND payload->>'review_attempt'=$4 AND status IN ('pending','claimed') LIMIT 1""",
            row["instance_id"], str(row["trigger_id"]), row["receipt_revision"], str(row["review_attempt"]),
        )
        if pending:
            await self._park(row, "awaiting_review", "Worker report review is queued.", row["receipt_revision"])
        elif row["review_attempt"] >= 3:
            await self._finish(row["trigger_id"], "failed", "Worker report review failed three times.")
        else:
            await self._queue_review(row, row["receipt_revision"])

    async def _invalid(self, row):
        count = await self.db.fetchval(
            """UPDATE aios.character_reminder_continuation
               SET invalid_decisions=invalid_decisions+1,
                   status=CASE WHEN invalid_decisions>=2 THEN 'failed' ELSE 'pending' END,
                   next_evaluation_at=now()+CASE WHEN invalid_decisions=0
                       THEN interval '30 seconds' ELSE interval '2 minutes' END,
                   decision_note='Invalid continuation decision'
               WHERE trigger_id=$1 AND status=ANY($2::text[]) RETURNING invalid_decisions""",
            row["trigger_id"], list(ACTIVE_STATES),
        )
        return count

    async def _park(self, row, status, note, signature, base_delay=60):
        unchanged = min(4, int(row.get("unchanged_checks", 0)) + 1) if row.get("wait_signature") == signature else 0
        delay = min(900, base_delay * 2 ** unchanged)
        await self.db.execute(
            """UPDATE aios.character_reminder_continuation
               SET status=$2,decision_note=$3,wait_signature=$4,unchanged_checks=$5,
                   next_evaluation_at=now()+make_interval(secs=>$6)
               WHERE trigger_id=$1 AND status=$7""",
            row["trigger_id"], status, note, signature, unchanged, delay, row["status"],
        )

    async def projects(self, instance_id, timeline_id):
        from .work_evidence import WorkEvidence

        rows = await self.db.fetch(
            """SELECT DISTINCT ON (c.goal_id) c.*,g.goal_text,p.revision,
                      r.status AS request_status,w.status AS run_status,next_wake.due_at AS next_reminder_at
               FROM aios.character_reminder_continuation c
               JOIN aios.character_agent_goal g USING(instance_id,goal_id)
               JOIN aios.character_goal_attempt a
                 ON a.attempt_id=c.attempt_id AND a.closed_at IS NULL
               LEFT JOIN aios.character_goal_plan p ON p.plan_id=c.plan_id
               LEFT JOIN aios.character_work_request r ON r.request_id=c.work_request_id
               LEFT JOIN aios.workspace_run w USING(run_id)
               LEFT JOIN LATERAL (
                 SELECT t.due_at FROM aios.character_temporal_trigger t
                 WHERE t.instance_id=c.instance_id AND t.payload->>'reminder_goal_id'=c.goal_id::text
                   AND t.payload->>'source_timeline_id'=c.source_timeline_id::text
                   AND (t.payload->>'attempt_id' IS NULL OR t.payload->>'attempt_id'=c.attempt_id::text)
                   AND t.status IN ('scheduled','firing','fired')
                 ORDER BY t.due_at LIMIT 1
               ) next_wake ON true
               WHERE c.instance_id=$1 AND c.source_timeline_id=$2 AND g.status='active'
               ORDER BY c.goal_id,c.created_at DESC LIMIT 3""", instance_id, timeline_id,
        )
        evidence = WorkEvidence(self.db)
        out = []
        for row in rows:
            reports = await evidence.planning(instance_id, row["goal_id"], row["attempt_id"])
            report = next((r for r in reports if r["goal_linked"]), None)
            out.append({
                "goal": row["goal_text"], "status": row["status"], "work_status": row["run_status"] or row["request_status"],
                "plan_revision": row["revision"],
                "note": row["decision_note"] if row["status"] not in {"waiting_work", "awaiting_review"} else None,
                "next_reminder_at": row["next_reminder_at"].isoformat() if row["next_reminder_at"] else None,
                "worker_report": report["summary"] if report else None,
            })
        return out


    async def _finish(self, trigger_id, status, note):
        await self.db.execute(
            """UPDATE aios.character_reminder_continuation SET status=$2,decision_note=$3
               WHERE trigger_id=$1 AND status=ANY($4::text[])""",
            trigger_id, status, note, list(ACTIVE_STATES),
        )


def work_proposal(raw, profiles, row, source_node_id):
    if (not isinstance(raw, dict) or set(raw) != {"decision", "capability", "purpose", "objective"}
            or raw["decision"] != "request_work"
            or not any(raw["capability"] == p["capability"] and raw["purpose"] == p["purpose"] for p in profiles)):
        raise ValueError("Invalid work proposal")
    return WorkRequestIn(
        request_id=request_id(row["trigger_id"], row.get("generation", 0)), source_node_id=source_node_id,
        goal_ref=row["goal_id"], capability=raw["capability"], purpose=raw["purpose"], objective=raw["objective"],
    )
