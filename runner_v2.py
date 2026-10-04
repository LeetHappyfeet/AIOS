from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict

from aios_app import runner as base
from aios_app.config import settings
from aios_app.db import Database
from aios_app.epistemic.topology_projection import (
    enqueue_dirty_scope_jobs,
    project_semantic_scope,
)
from aios_app.pipeline.jobs import enqueue_job

logger = logging.getLogger("aios.pipeline.runner")

RDF_PROJECTION_SCHEDULER_SECONDS = 5.0
COGNITION_RECOVERY_SCHEDULER_SECONDS = 60.0


async def handle_project_semantic_scope(db: Database, job: Dict[str, Any]) -> None:
    scope_key = str((job.get("payload") or {}).get("scope_key") or "").strip()
    if not scope_key:
        raise ValueError("project_semantic_scope requires scope_key")
    fuseki = base.FusekiClient(settings.fuseki_base_url)
    await project_semantic_scope(db, fuseki, scope_key=scope_key)


base.JOB_HANDLERS["project_semantic_scope"] = handle_project_semantic_scope


async def handle_compact_character_world_epistemic(db: Database, job: Dict[str, Any]) -> None:
    from aios_app.rdf.epistemic_writer import compact_character_world_epistemic_shadows
    fuseki = base.FusekiClient(settings.fuseki_base_url)
    await compact_character_world_epistemic_shadows(db, fuseki, limit=100)


base.JOB_HANDLERS["compact_character_world_epistemic"] = handle_compact_character_world_epistemic

async def handle_semantic_hygiene_shadow(db: Database, job: Dict[str, Any]) -> None:
    """Read-only /char inspection; writes ONLY semantic_hygiene_shadow_audit/cursor."""
    from aios_app.epistemic.semantic_hygiene import run_shadow_batch
    population = str((job.get("payload") or {}).get("population") or "v3_only")
    fuseki = base.FusekiClient(settings.fuseki_base_url, timeout=12.0, retries=0)
    await run_shadow_batch(db, fuseki, population=population, limit=16)


base.JOB_HANDLERS["semantic_hygiene_shadow"] = handle_semantic_hygiene_shadow


async def handle_agent_wake(db: Database, job: Dict[str, Any]) -> None:
    from uuid import UUID
    from aios_app.agent.autonomy import AutonomyScheduler
    from aios_app.agent.worker import CharacterWorker
    task_id = job.get("payload", {}).get("task_id")
    if not task_id:
        raise ValueError("agent_wake requires task_id")
    task_uuid = UUID(str(task_id))
    try:
        await CharacterWorker(db).run_task(task_uuid)
    finally:
        await AutonomyScheduler(db).consume_task_wakes(task_uuid)

base.JOB_HANDLERS["agent_wake"] = handle_agent_wake

async def handle_cognitive_operation(db: Database, job: Dict[str, Any]) -> None:
    from uuid import UUID
    from aios_app.agent.cognitive_operations import CognitiveOperationEngine
    operation_id=job.get("payload",{}).get("operation_id")
    if not operation_id:
        raise ValueError("cognitive_operation requires operation_id")
    await CognitiveOperationEngine(db).execute(UUID(str(operation_id)))

base.JOB_HANDLERS["cognitive_operation"] = handle_cognitive_operation

async def handle_internal_cognition_inference(db: Database, job: Dict[str, Any]) -> None:
    from uuid import UUID
    from aios_app.agent.transactions import InternalCognitionTransactions
    transaction_id=job.get("payload",{}).get("transaction_id")
    if not transaction_id:
        raise ValueError("internal_cognition_inference requires transaction_id")
    await InternalCognitionTransactions(db).run(UUID(str(transaction_id)))

base.JOB_HANDLERS["internal_cognition_inference"] = handle_internal_cognition_inference

async def handle_goal_formulation_inference(db: Database, job: Dict[str, Any]) -> None:
    from uuid import UUID
    from aios_app.agent.cognitive_operations import CognitiveOperationEngine
    operation_id=job.get("payload",{}).get("operation_id")
    if not operation_id:
        raise ValueError("goal_formulation_inference requires operation_id")
    await CognitiveOperationEngine(db).form_goal(UUID(str(operation_id)))

base.JOB_HANDLERS["goal_formulation_inference"] = handle_goal_formulation_inference


async def handle_character_inquiry_inference(db: Database, job: Dict[str, Any]) -> None:
    from uuid import UUID
    from aios_app.agent.cognitive_operations import CognitiveOperationEngine
    operation_id = (job.get("payload") or {}).get("operation_id")
    if not operation_id:
        raise ValueError("character_inquiry_inference requires operation_id")
    await CognitiveOperationEngine(db).complete_inquiry(UUID(str(operation_id)))


base.JOB_HANDLERS["character_inquiry_inference"] = handle_character_inquiry_inference


async def handle_character_inquiry_shadow(db: Database, job: Dict[str, Any]) -> None:
    from uuid import UUID
    from aios_app.epistemic.inquiry.shadow import scan_v11_rejections
    payload = job.get("payload") or {}
    if not payload.get("instance_id") or not payload.get("node_id"):
        raise ValueError("character_inquiry_shadow needs instance_id and node_id")
    await scan_v11_rejections(
        db, instance_id=UUID(str(payload["instance_id"])),
        node_id=UUID(str(payload["node_id"])))


base.JOB_HANDLERS["character_inquiry_shadow"] = handle_character_inquiry_shadow

async def handle_message_cognition_enrichment(db: Database, job: Dict[str, Any]) -> None:
    from uuid import UUID
    from aios_app.epistemic.message_cognition_enrichment import MessageCognitionEnricher
    payload=job.get("payload") or {}
    if not payload.get("instance_id") or not payload.get("node_id"):
        raise ValueError("message_cognition_enrichment requires instance_id and node_id")
    await MessageCognitionEnricher(db).run(
        instance_id=UUID(str(payload["instance_id"])),
        node_id=UUID(str(payload["node_id"])))
    # Post-enrichment shadow diagnostic is independently queued; lookup never
    # blocks parser admission or historical completion.
    try:
        completed = await db.fetchrow(
            """SELECT summary FROM aios.message_cognitive_commit
               WHERE instance_id=$1 AND node_id=$2
                 AND enrichment_completed_at IS NOT NULL""",
            UUID(str(payload["instance_id"])),UUID(str(payload["node_id"])))
        summary = str(completed["summary"]) if completed else ""
        if ("source_admission:unresolved_reference:" in summary or
                "source_admission:attribution_unresolved:" in summary):
            await enqueue_job(
                db,job_type="character_inquiry_shadow",
                payload={"instance_id":str(payload["instance_id"]),
                         "node_id":str(payload["node_id"])},priority=35)
    except Exception:
        logger.exception("Optional source inquiry shadow enqueue failed")

base.JOB_HANDLERS["message_cognition_enrichment"] = handle_message_cognition_enrichment


async def handle_message_cognition_catchup(db: Database, job: Dict[str, Any]) -> None:
    from uuid import UUID
    from aios_app.epistemic.cognition_catchup import recover_missing_cognition
    payload = job.get("payload") or {}
    if not payload.get("instance_id"):
        raise ValueError("message_cognition_catchup requires instance_id")
    instance_id = UUID(str(payload["instance_id"]))
    # Do not infer the current goal state while a missing earlier source turn
    # or a deferred model review may still contain a later withdrawal.
    for _ in range(8):
        report = await recover_missing_cognition(db, instance_id=instance_id, limit=32)
        if not report["more_possible"] or report["committed"] == 0:
            break
    from aios_app.epistemic.cognition_catchup import finish_deferred_cognition
    outcome = None
    for _ in range(2):
        outcome = await finish_deferred_cognition(db, instance_id=instance_id)
        logger.info("Cognition history completion instance=%s result=%s",
                    instance_id, outcome)
        if outcome["status"] not in {
            "source_inference_pending", "goal_reconciliation_pending"
        }:
            break
        if outcome["status"] == "source_inference_pending" and not outcome["enrichment_completed"]:
            break
        reviewed = outcome.get("goal_reconciliation") or {}
        if outcome["status"] == "goal_reconciliation_pending" and not reviewed.get("considered"):
            break
    # A completed pipeline job cannot resume itself after DNS or provider
    # recovery. Schedule bounded, delayed continuation without tight loops.
    if outcome and outcome["status"] in {
        "source_inference_running", "source_inference_pending",
        "source_inference_failed", "source_inference_invalid_response",
        "source_inference_unavailable", "goal_reconciliation_pending",
    }:
        from datetime import datetime, timedelta, timezone
        retry_count = max(0, min(12, int(payload.get("retry_count") or 0)))
        delay = min(900, 60 * (2 ** min(retry_count, 4)))
        if outcome["status"] == "source_inference_running":
            delay = max(120, delay)
        existing = await db.fetchrow(
            """SELECT job_id FROM aios.pipeline_job
               WHERE job_type='message_cognition_catchup'
                 AND payload->>'instance_id'=$1
                 AND status='queued'
               LIMIT 1""", str(instance_id),
        )
        if not existing:
            await enqueue_job(
                db, job_type="message_cognition_catchup",
                payload={"instance_id": str(instance_id),
                         "retry_count": min(retry_count + 1, 12)},
                priority=65,
                run_after=datetime.now(timezone.utc) + timedelta(seconds=delay),
            )

base.JOB_HANDLERS["message_cognition_catchup"] = handle_message_cognition_catchup

_original_resolve_partition_key = base._resolve_partition_key


async def _resolve_partition_key(db: Database, job: Dict[str, Any]) -> str:
    payload = job.get("payload") or {}
    job_type = str(job.get("job_type") or "")
    if job_type in {"agent_wake","cognitive_operation","internal_cognition_inference","goal_formulation_inference","message_cognition_enrichment", "message_cognition_catchup", "character_inquiry_shadow", "character_inquiry_inference"} and payload.get("instance_id"):
        return "instance:" + str(payload["instance_id"])
    if job_type == "project_semantic_scope" and payload.get("scope_key"):
        return str(payload["scope_key"])
    if job_type == "project_character_knowledge" and payload.get("live_instance_id"):
        return f"instance:{payload['live_instance_id']}"
    return await _original_resolve_partition_key(db, job)


base._resolve_partition_key = _resolve_partition_key


def _emit_telemetry(payload: dict[str, Any]) -> None:
    print(
        "AIOS_TELEMETRY " + json.dumps(payload, separators=(",", ":"), default=str),
        flush=True,
    )


def _short(value: Any, width: int = 8) -> str:
    text = str(value or "")
    return text[:width] if text else ""


def _decode_payload(value: Any) -> dict[str, Any]:
    """Normalize pipeline_job.payload for telemetry-only inspection.

    Depending on the asyncpg/PostgreSQL JSON codec configuration, payload can
    arrive as a mapping, a JSON string, bytes, or NULL. Telemetry should never
    fail the runner because of representation differences.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "items"):
        try:
            return dict(value.items())
        except Exception:
            return {}
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


async def _work_timeline(db: Database, row: Any) -> dict[str, Any]:
    """Resolve the causal DAG timeline for telemetry without inferring one.

    A character identity is not enough to assign a timeline: claim/node/section
    work must be linked through the DAG.  That distinction makes orphaned work
    visible as NO TIMELINE instead of making it look like normal character work.
    """
    payload = _decode_payload(row["payload"])

    explicit_timeline = payload.get("timeline_id")
    if explicit_timeline:
        return {"timeline_status": "LINKED", "timeline_id": str(explicit_timeline)}

    node_id = payload.get("node_id")
    if node_id:
        resolved = await db.fetchrow(
            "SELECT timeline_id FROM aios.dag_node WHERE node_id=$1::uuid",
            node_id,
        )
        timeline_id = resolved["timeline_id"] if resolved else None
        return {
            "timeline_status": "LINKED" if timeline_id else "MISSING",
            "timeline_id": str(timeline_id) if timeline_id else None,
        }

    section_id = payload.get("section_id")
    if section_id:
        resolved = await db.fetchrow(
            """
            SELECT n.timeline_id
            FROM aios.document_section ds
            LEFT JOIN aios.dag_node n ON n.node_id=ds.node_id
            WHERE ds.section_id=$1::uuid
            """,
            section_id,
        )
        timeline_id = resolved["timeline_id"] if resolved else None
        return {
            "timeline_status": "LINKED" if timeline_id else "MISSING",
            "timeline_id": str(timeline_id) if timeline_id else None,
        }

    claim_id = payload.get("claim_id")
    if claim_id:
        resolved = await db.fetchrow(
            """
            SELECT n.timeline_id
            FROM aios.claim_candidate cc
            LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
            LEFT JOIN aios.dag_node n ON n.node_id=ds.node_id
            WHERE cc.claim_id=$1::uuid
            """,
            claim_id,
        )
        timeline_id = resolved["timeline_id"] if resolved else None
        return {
            "timeline_status": "LINKED" if timeline_id else "MISSING",
            "timeline_id": str(timeline_id) if timeline_id else None,
        }

    acquisition_id = payload.get("acquisition_id")
    if acquisition_id:
        resolved = await db.fetchrow(
            """
            SELECT n.timeline_id
            FROM aios.knowledge_acquisition_event kae
            LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
            LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
            LEFT JOIN aios.dag_node n ON n.node_id=ds.node_id
            WHERE kae.acquisition_id=$1::uuid
            """,
            acquisition_id,
        )
        timeline_id = resolved["timeline_id"] if resolved else None
        return {
            "timeline_status": "LINKED" if timeline_id else "MISSING",
            "timeline_id": str(timeline_id) if timeline_id else None,
        }

    return {"timeline_status": "NOT_APPLICABLE", "timeline_id": None}


async def _work_subject(db: Database, row: Any) -> dict[str, Any]:
    payload = _decode_payload(row["payload"])
    partition_key = str(row["partition_key"] or "")

    scope_key = str(payload.get("scope_key") or "")
    for value in (scope_key, partition_key):
        if value.startswith("char:"):
            return {"subject_type": "character", "subject_id": value.split(":", 1)[1]}
        if value.startswith("world:"):
            return {"subject_type": "world", "subject_id": value}
        if value.startswith("source:"):
            return {"subject_type": "source", "subject_id": value.split(":", 1)[1]}

    if payload.get("character_id"):
        return {"subject_type": "character", "subject_id": str(payload["character_id"])}

    live_instance_id = payload.get("live_instance_id") or payload.get("instance_id")
    if live_instance_id:
        resolved = await db.fetchrow(
            """
            SELECT character_id
            FROM aios.character_instance
            WHERE instance_id=$1::uuid
            """,
            live_instance_id,
        )
        if resolved and resolved["character_id"]:
            return {
                "subject_type": "character",
                "subject_id": str(resolved["character_id"]),
                "instance_id": str(live_instance_id),
            }

    acquisition_id = payload.get("acquisition_id")
    if acquisition_id:
        resolved = await db.fetchrow(
            """
            SELECT ci.character_id
            FROM aios.knowledge_acquisition_event kae
            JOIN aios.character_instance ci ON ci.instance_id=kae.instance_id
            WHERE kae.acquisition_id=$1::uuid
            """,
            acquisition_id,
        )
        if resolved and resolved["character_id"]:
            return {"subject_type": "character", "subject_id": str(resolved["character_id"])}

    claim_id = payload.get("claim_id")
    if claim_id:
        resolved = await db.fetchrow(
            """
            SELECT origin_character_id, world_id, source_id
            FROM aios.claim_context_resolution
            WHERE claim_id=$1::uuid
            """,
            claim_id,
        )
        if resolved:
            if resolved["origin_character_id"]:
                return {
                    "subject_type": "character",
                    "subject_id": str(resolved["origin_character_id"]),
                }
            if resolved["world_id"]:
                return {
                    "subject_type": "world",
                    "subject_id": f"world:{resolved['world_id']}",
                }
            if resolved["source_id"]:
                return {"subject_type": "source", "subject_id": str(resolved["source_id"])}

    for key, subject_type in (
        ("world_id", "world"),
        ("node_id", "node"),
        ("section_id", "section"),
        ("claim_id", "claim"),
        ("assertion_id", "assertion"),
        ("acquisition_id", "acquisition"),
    ):
        if payload.get(key):
            value = str(payload[key])
            return {"subject_type": subject_type, "subject_id": value, "short_id": _short(value)}

    if partition_key:
        return {"subject_type": "partition", "subject_id": partition_key}
    return {"subject_type": "global", "subject_id": "global"}


async def _describe_work_rows(db: Database, rows: list[Any]) -> list[dict[str, Any]]:
    described: list[dict[str, Any]] = []
    for row in rows:
        subject = await _work_subject(db, row)
        timeline = await _work_timeline(db, row)
        described.append(
            {
                "job_id": str(row["job_id"]),
                "job_type": str(row["job_type"]),
                "resource": str(row["resource_class"]),
                "lane": str(row["scheduling_lane"]),
                "running_s": round(float(row.get("running_seconds", 0.0) or 0.0), 2)
                if hasattr(row, "get")
                else round(float(row["running_seconds"] or 0.0), 2),
                **subject,
                **timeline,
            }
        )
    return described


def _candidate_state(
    *,
    queued: int,
    oldest_s: float,
    arrivals_per_s: float,
    done_per_s: float,
    queue_velocity: float,
    failures: int = 0,
    live: bool = False,
) -> str:
    if failures:
        return "DEGRADED"
    if queued == 0 or (live and oldest_s <= 2.0):
        return "READY" if live else "CAUGHT_UP"

    scale = max(arrivals_per_s, done_per_s, 0.25)
    net_ratio = (done_per_s - arrivals_per_s) / scale
    if net_ratio >= 0.10 and queue_velocity <= 0.10:
        return "DRAINING"
    if net_ratio <= -0.10 and queue_velocity >= -0.10:
        return "LAGGING" if live else "FALLING_BEHIND"
    return "BUSY"


def _stabilize_state(
    key: str,
    candidate: str,
    stable: dict[str, str],
    pending: dict[str, tuple[str, int]],
) -> str:
    current = stable.get(key)
    if current is None:
        stable[key] = candidate
        return candidate
    if candidate == current:
        pending.pop(key, None)
        return current
    if candidate in {"DEGRADED", "CAUGHT_UP", "READY"}:
        stable[key] = candidate
        pending.pop(key, None)
        return candidate

    previous_candidate, count = pending.get(key, ("", 0))
    count = count + 1 if previous_candidate == candidate else 1
    pending[key] = (candidate, count)
    if count >= 3:
        stable[key] = candidate
        pending.pop(key, None)
    return stable[key]


async def _pipeline_telemetry_loop(interval: float = 5.0) -> None:
    """Publish compact queue, stage, and active-subject telemetry."""
    db = Database(
        settings.db_dsn,
        min_size=1,
        max_size=max(2, settings.db_pool_min_size),
    )
    await db.connect()
    previous_queued: int | None = None
    previous_live_queued: int | None = None
    previous_at: float | None = None
    stable_states: dict[str, str] = {}
    pending_states: dict[str, tuple[str, int]] = {}
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                rows = await db.fetch(
                    """
                    SELECT
                        resource_class,
                        scheduling_lane,
                        COUNT(*) FILTER (WHERE status='queued' AND run_after <= now())::integer AS queued,
                        COUNT(*) FILTER (WHERE status='running')::integer AS running,
                        COALESCE(
                            EXTRACT(EPOCH FROM (
                                now() - MIN(created_at) FILTER (
                                    WHERE status='queued' AND run_after <= now()
                                )
                            )),
                            0
                        )::double precision AS oldest_seconds
                    FROM aios.pipeline_job
                    WHERE status IN ('queued','running')
                    GROUP BY resource_class, scheduling_lane
                    ORDER BY resource_class, scheduling_lane
                    """
                )
                stage_rows = await db.fetch(
                    """
                    SELECT
                        job_type,
                        resource_class,
                        scheduling_lane,
                        COUNT(*) FILTER (WHERE status='queued' AND run_after <= now())::integer AS queued,
                        COUNT(*) FILTER (WHERE status='running')::integer AS running,
                        COALESCE(
                            EXTRACT(EPOCH FROM (
                                now() - MIN(created_at) FILTER (
                                    WHERE status='queued' AND run_after <= now()
                                )
                            )),
                            0
                        )::double precision AS oldest_seconds
                    FROM aios.pipeline_job
                    WHERE status IN ('queued','running')
                    GROUP BY job_type, resource_class, scheduling_lane
                    HAVING COUNT(*) FILTER (WHERE status='queued' AND run_after <= now()) > 0
                        OR COUNT(*) FILTER (WHERE status='running') > 0
                    ORDER BY oldest_seconds DESC, queued DESC
                    """
                )
                rates = await db.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE created_at >= now() - interval '30 seconds')::integer AS arrivals_30s,
                        COUNT(*) FILTER (
                            WHERE status='done' AND updated_at >= now() - interval '30 seconds'
                        )::integer AS done_30s,
                        COUNT(*) FILTER (
                            WHERE scheduling_lane='LIVE'
                              AND created_at >= now() - interval '30 seconds'
                        )::integer AS live_arrivals_30s,
                        COUNT(*) FILTER (
                            WHERE scheduling_lane='LIVE'
                              AND status='done'
                              AND updated_at >= now() - interval '30 seconds'
                        )::integer AS live_done_30s,
                        COUNT(*) FILTER (
                            WHERE status='failed' AND updated_at >= now() - interval '60 seconds'
                        )::integer AS failed_60s
                    FROM aios.pipeline_job
                    WHERE created_at >= now() - interval '60 seconds'
                       OR updated_at >= now() - interval '60 seconds'
                    """
                )
                running_rows = await db.fetch(
                    """
                    SELECT
                        job_id, job_type, resource_class, scheduling_lane,
                        partition_key, payload,
                        COALESCE(EXTRACT(EPOCH FROM (now() - claimed_at)), 0)::double precision AS running_seconds
                    FROM aios.pipeline_job
                    WHERE status='running'
                    ORDER BY claimed_at ASC NULLS LAST
                    LIMIT 12
                    """
                )
                recent_rows = await db.fetch(
                    """
                    SELECT
                        job_id, job_type, resource_class, scheduling_lane,
                        partition_key, payload,
                        COALESCE(EXTRACT(EPOCH FROM (now() - updated_at)), 0)::double precision AS running_seconds
                    FROM aios.pipeline_job
                    WHERE status='done'
                      AND updated_at >= now() - interval '20 seconds'
                    ORDER BY updated_at DESC
                    LIMIT 8
                    """
                )

                lanes: list[dict[str, Any]] = []
                total_queued = total_running = live_queued = live_running = 0
                oldest = live_oldest = 0.0
                for row in rows:
                    queued = int(row["queued"] or 0)
                    running = int(row["running"] or 0)
                    age = float(row["oldest_seconds"] or 0.0)
                    lane = str(row["scheduling_lane"])
                    total_queued += queued
                    total_running += running
                    oldest = max(oldest, age)
                    if lane == "LIVE":
                        live_queued += queued
                        live_running += running
                        live_oldest = max(live_oldest, age)
                    lanes.append(
                        {
                            "resource": str(row["resource_class"]),
                            "lane": lane,
                            "queued": queued,
                            "running": running,
                            "oldest_s": round(age, 2),
                        }
                    )

                now = loop.time()
                queue_velocity = live_queue_velocity = 0.0
                if previous_at is not None:
                    elapsed = max(0.001, now - previous_at)
                    if previous_queued is not None:
                        queue_velocity = (total_queued - previous_queued) / elapsed
                    if previous_live_queued is not None:
                        live_queue_velocity = (live_queued - previous_live_queued) / elapsed
                previous_queued = total_queued
                previous_live_queued = live_queued
                previous_at = now

                arrivals_per_s = float((rates["arrivals_30s"] if rates else 0) or 0) / 30.0
                done_per_s = float((rates["done_30s"] if rates else 0) or 0) / 30.0
                live_arrivals_per_s = float((rates["live_arrivals_30s"] if rates else 0) or 0) / 30.0
                live_done_per_s = float((rates["live_done_30s"] if rates else 0) or 0) / 30.0
                failed_60s = int((rates["failed_60s"] if rates else 0) or 0)

                state = _stabilize_state(
                    "global",
                    _candidate_state(
                        queued=total_queued,
                        oldest_s=oldest,
                        arrivals_per_s=arrivals_per_s,
                        done_per_s=done_per_s,
                        queue_velocity=queue_velocity,
                        failures=failed_60s,
                    ),
                    stable_states,
                    pending_states,
                )
                live_state = _stabilize_state(
                    "live",
                    _candidate_state(
                        queued=live_queued,
                        oldest_s=live_oldest,
                        arrivals_per_s=live_arrivals_per_s,
                        done_per_s=live_done_per_s,
                        queue_velocity=live_queue_velocity,
                        live=True,
                    ),
                    stable_states,
                    pending_states,
                )

                stage_backlog = [
                    {
                        "job_type": str(row["job_type"]),
                        "resource": str(row["resource_class"]),
                        "lane": str(row["scheduling_lane"]),
                        "queued": int(row["queued"] or 0),
                        "running": int(row["running"] or 0),
                        "oldest_s": round(float(row["oldest_seconds"] or 0.0), 2),
                    }
                    for row in stage_rows
                ]
                active_work = await _describe_work_rows(db, list(running_rows))
                recent_work = await _describe_work_rows(db, list(recent_rows))

                _emit_telemetry(
                    {
                        "service": "pipeline",
                        "state": state,
                        "live_state": live_state,
                        "queued": total_queued,
                        "running": total_running,
                        "oldest_s": round(oldest, 2),
                        "live_queued": live_queued,
                        "live_running": live_running,
                        "live_oldest_s": round(live_oldest, 2),
                        "arrivals_per_s": round(arrivals_per_s, 2),
                        "done_per_s": round(done_per_s, 2),
                        "live_arrivals_per_s": round(live_arrivals_per_s, 2),
                        "live_done_per_s": round(live_done_per_s, 2),
                        "queue_velocity_per_s": round(queue_velocity, 2),
                        "live_queue_velocity_per_s": round(live_queue_velocity, 2),
                        "failed_60s": failed_60s,
                        "lanes": lanes,
                        "stage_backlog": stage_backlog,
                        "active_work": active_work,
                        "recent_work": recent_work,
                    }
                )
            except Exception:
                logger.exception("Failed to collect pipeline telemetry")
            await asyncio.sleep(interval)
    finally:
        await db.close()


async def _projection_scheduler_loop() -> None:
    db = Database(
        settings.db_dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    await db.connect()
    try:
        while True:
            try:
                created = await enqueue_dirty_scope_jobs(db, limit=64)
                if created:
                    logger.debug("Queued %d coalesced semantic scope projections", created)

                cleanup_needed = await db.fetchrow(
                    """
                    SELECT 1
                    FROM aios.rdf_promotion_log rpl
                    JOIN aios.claim_context_resolution ccr ON ccr.claim_id=rpl.claim_id
                    WHERE rpl.rdf_dataset='world'
                      AND rpl.rdf_graph='urn:aios:world:epistemic'
                      AND rpl.rdf_predicate='world:observesProposition'
                      AND ccr.origin_character_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM aios.pipeline_job pj
                          WHERE pj.job_type='compact_character_world_epistemic'
                            AND pj.status IN ('queued','running')
                      )
                    LIMIT 1
                    """
                )
                if cleanup_needed:
                    await enqueue_job(
                        db,
                        job_type="compact_character_world_epistemic",
                        payload={},
                        priority=250,
                    )

                # Explicit opt-in; at most one small shadow batch per minute.
                # This must never compete continuously with live RDF projection.
                if os.getenv("AIOS_SEMANTIC_HYGIENE_SHADOW_ENABLED", "0").lower() in {
                    "1", "true", "yes", "on",
                }:
                    from aios_app.epistemic.semantic_hygiene import POLICY_VERSION
                    hygiene_state = await db.fetchrow(
                        """
                        SELECT population
                        FROM aios.semantic_hygiene_shadow_cursor
                        WHERE policy_version=$1 AND completed_at IS NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM aios.pipeline_job pj
                            WHERE pj.job_type='semantic_hygiene_shadow'
                              AND (pj.status IN ('queued','running')
                                OR pj.created_at > now() - interval '60 seconds')
                          )
                        ORDER BY CASE WHEN population='v3_only' THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        POLICY_VERSION,
                    )
                    if hygiene_state:
                        await enqueue_job(
                            db,
                            job_type="semantic_hygiene_shadow",
                            payload={"population": str(hygiene_state["population"])},
                            priority=290,
                        )
            except Exception:
                logger.exception("Failed to schedule dirty semantic topology scopes")
            await asyncio.sleep(RDF_PROJECTION_SCHEDULER_SECONDS)
    finally:
        await db.close()



async def _deferred_cognition_recovery_loop(
    interval: float = COGNITION_RECOVERY_SCHEDULER_SECONDS,
) -> None:
    """Independent restart recovery: no dependency on a surviving catch-up job."""
    from aios_app.epistemic.cognition_recovery_sweep import enqueue_abandoned_enrichment
    from aios_app.epistemic.inquiry.shadow import enqueue_v11_inquiry_shadow
    from aios_app.inference.providers import InferenceProviderStore

    db = Database(settings.db_dsn, min_size=1, max_size=2)
    await db.connect()
    try:
        while True:
            try:
                # Inference leases are reclaimed even when no catch-up is
                # currently executing; only expired leases may be mutated.
                reaped = await InferenceProviderStore(db).reap_stale_requests()
                enqueued = await enqueue_abandoned_enrichment(db, limit=16)
                shadowed = await enqueue_v11_inquiry_shadow(db, limit=8)
                if reaped or enqueued or shadowed:
                    logger.info(
                        "Deferred cognition sweep reaped=%s enqueued=%s inquiry=%s",
                        reaped, enqueued, shadowed,
                    )
            except Exception:
                logger.exception("Failed deferred cognition recovery sweep")
            await asyncio.sleep(interval)
    finally:
        await db.close()


async def run_runner(poll_interval: float = 1.0) -> None:
    projector = asyncio.create_task(
        _projection_scheduler_loop(),
        name="semantic-scope-projection-scheduler",
    )
    telemetry = asyncio.create_task(
        _pipeline_telemetry_loop(),
        name="pipeline-telemetry",
    )
    cognition_recovery = asyncio.create_task(
        _deferred_cognition_recovery_loop(),
        name="deferred-cognition-recovery-scheduler",
    )
    try:
        await base.run_runner(poll_interval=poll_interval)
    finally:
        projector.cancel()
        telemetry.cancel()
        cognition_recovery.cancel()
        await asyncio.gather(
            projector, telemetry, cognition_recovery, return_exceptions=True,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_runner())
