# aios_app/runner.py

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Callable, Awaitable, Dict, Any
from uuid import UUID

from aios_app.config import settings
from aios_app.db import Database
from aios_app.pipeline.jobs import (
    fetch_next_job,
    heartbeat_job,
    mark_done,
    mark_failed,
    recover_stale_running_jobs,
    rebalance_queued_priorities,
)
from aios_app.pipeline.job_registry import ResourceClass, SchedulingLane, job_spec

from aios_app.pipeline.dag_to_document_section_worker import run_worker as run_dag_to_document_section
from aios_app.pipeline.worker import run_claim_extraction_for_section
from aios_app.char.discover_characters_worker import run_worker as run_discover_character

from aios_app.rdf.fuseki import FusekiClient
from aios_app.rdf.world_liminal import promote_liminal_claims
from aios_app.rdf.world_liminal_classifier_runner import classify_liminal_claims
from aios_app.rdf.epistemic_writer import project_normalized_observation
from aios_app.epistemic.normalizer import normalize_claim_once
from aios_app.epistemic.context_resolver import resolve_claim_context
from aios_app.epistemic.narratives import assign_narratives_once
from aios_app.epistemic.knowledge import project_knowledge_acquisitions_once
from aios_app.epistemic.generated import resolve_generated_facts_once
from aios_app.epistemic.topology import derive_claim_topology, derive_world_assertion_topology, derive_character_acquisition_topology
from aios_app.world.topology import project_world_topology

logger = logging.getLogger("aios.pipeline.runner")

JOB_HANDLERS: Dict[str, Callable[[Database, Dict[str, Any]], Awaitable[None]]] = {}


# -------------------------------------------------
# Existing handlers
# -------------------------------------------------

async def handle_discover_characters(db: Database, job: Dict[str, Any]) -> None:
    character_id = str(job["payload"]["character_id"]).strip()
    if not character_id:
        logger.warning("Skipping discover_characters job with empty character_id")
        return
    await run_discover_character(db, character_id=character_id)


async def handle_project_world_topology(db: Database, job: Dict[str, Any]) -> None:
    world_id = UUID(job["payload"]["world_id"])
    fuseki = FusekiClient(settings.fuseki_base_url)
    await project_world_topology(db, fuseki, world_id=world_id)


async def handle_dag_to_document_section(db: Database, job: Dict[str, Any]) -> None:
    node_id = UUID(job["payload"]["node_id"])
    await run_dag_to_document_section(db, node_id=node_id)


async def handle_extract_claims(db: Database, job: Dict[str, Any]) -> None:
    section_id = UUID(job["payload"]["section_id"])
    await run_claim_extraction_for_section(db, section_id=section_id)


# -------------------------------------------------
# Epistemic handlers
# -------------------------------------------------

async def handle_normalize_proposition(db: Database, job: Dict[str, Any]) -> None:
    claim_id = UUID(job["payload"]["claim_id"])
    exists = await db.fetchrow(
        "SELECT 1 FROM aios.claim_candidate WHERE claim_id=$1",
        claim_id,
    )
    if not exists:
        logger.warning(
            "Skipping stale normalize_proposition job for missing claim %s",
            claim_id,
        )
        return
    context = await db.fetchrow(
        "SELECT 1 FROM aios.claim_context_resolution WHERE claim_id=$1",
        claim_id,
    )
    if not context:
        logger.info(
            "Deferring normalize_proposition for claim %s until context resolution",
            claim_id,
        )
        return
    await normalize_claim_once(db, claim_id=claim_id)


async def handle_rdf_epistemic_project(db: Database, job: Dict[str, Any]) -> None:
    claim_id = UUID(job["payload"]["claim_id"])
    exists = await db.fetchrow(
        "SELECT 1 FROM aios.observation WHERE claim_id=$1",
        claim_id,
    )
    if not exists:
        logger.warning(
            "Skipping stale rdf_epistemic_project job for missing observation claim %s",
            claim_id,
        )
        return
    context = await db.fetchrow(
        "SELECT 1 FROM aios.claim_context_resolution WHERE claim_id=$1",
        claim_id,
    )
    if not context:
        logger.info(
            "Deferring rdf_epistemic_project for claim %s until context resolution",
            claim_id,
        )
        return

    fuseki = FusekiClient(settings.fuseki_base_url)
    await project_normalized_observation(
        db,
        fuseki,
        claim_id=claim_id,
    )


async def handle_derive_claim_topology(db: Database, job: Dict[str, Any]) -> None:
    claim_id = UUID(job["payload"]["claim_id"])
    fuseki = FusekiClient(settings.fuseki_base_url)
    await derive_claim_topology(db, fuseki, claim_id=claim_id)


async def handle_derive_world_assertion_topology(db: Database, job: Dict[str, Any]) -> None:
    assertion_id = UUID(job["payload"]["assertion_id"])
    fuseki = FusekiClient(settings.fuseki_base_url)
    await derive_world_assertion_topology(db, fuseki, assertion_id=assertion_id)


async def handle_derive_character_acquisition_topology(db: Database, job: Dict[str, Any]) -> None:
    acquisition_id = UUID(job["payload"]["acquisition_id"])
    fuseki = FusekiClient(settings.fuseki_base_url)
    await derive_character_acquisition_topology(db, fuseki, acquisition_id=acquisition_id)


async def handle_assign_narratives(db: Database, job: Dict[str, Any]) -> None:
    await assign_narratives_once(db, limit=500)


async def handle_project_character_knowledge(db: Database, job: Dict[str, Any]) -> None:
    live_instance_id = (job.get("payload") or {}).get("live_instance_id")
    await project_knowledge_acquisitions_once(
        db,
        limit=500,
        instance_id=UUID(live_instance_id) if live_instance_id else None,
    )


async def handle_resolve_generated_facts(db: Database, job: Dict[str, Any]) -> None:
    await resolve_generated_facts_once(db, limit=200)


# -------------------------------------------------
# RDF handlers
# -------------------------------------------------

async def handle_rdf_liminal_promote(db: Database, job: Dict[str, Any]) -> None:
    section_id = UUID(job["payload"]["section_id"])
    fuseki = FusekiClient(settings.fuseki_base_url)
    await promote_liminal_claims(
        db,
        fuseki,
        section_id=section_id,
        batch_size=500,
    )


async def handle_rdf_liminal_classify(db: Database, job: Dict[str, Any]) -> None:
    fuseki = FusekiClient(settings.fuseki_base_url)
    await classify_liminal_claims(db, fuseki, batch_size=500)


async def handle_resolve_claim_context(db: Database, job: Dict[str, Any]) -> None:
    claim_id = UUID(job["payload"]["claim_id"])
    linked = await db.fetchrow(
        """
        SELECT 1
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es
          ON es.sentence_id = cc.sentence_id
        JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        WHERE cc.claim_id = $1
        """,
        claim_id,
    )
    if not linked:
        logger.warning(
            "Skipping stale resolve_claim_context job for missing or unlinked claim %s",
            claim_id,
        )
        return
    fuseki = FusekiClient(settings.fuseki_base_url)
    await resolve_claim_context(db, fuseki, claim_id=claim_id)


JOB_HANDLERS.update(
    {
        "discover_characters": handle_discover_characters,
        "project_world_topology": handle_project_world_topology,
        "dag_to_document_section": handle_dag_to_document_section,
        "extract_claims": handle_extract_claims,
        "rdf_liminal_promote": handle_rdf_liminal_promote,
        "rdf_liminal_classify": handle_rdf_liminal_classify,
        "resolve_claim_context": handle_resolve_claim_context,
        "normalize_proposition": handle_normalize_proposition,
        "rdf_epistemic_project": handle_rdf_epistemic_project,
        "derive_claim_topology": handle_derive_claim_topology,
        "derive_world_assertion_topology": handle_derive_world_assertion_topology,
        "derive_character_acquisition_topology": handle_derive_character_acquisition_topology,
        "assign_narratives": handle_assign_narratives,
        "project_character_knowledge": handle_project_character_knowledge,
        "resolve_generated_facts": handle_resolve_generated_facts,
    }
)


async def _mark_origin_event_error(
    db: Database,
    *,
    job_type: str,
    payload: Dict[str, Any],
    error: str,
) -> None:
    """Propagate downstream worker failure to the source ingest_event."""

    if job_type == "dag_to_document_section" and payload.get("node_id"):
        await db.execute(
            """
            UPDATE aios.ingest_event ie
            SET process_status = 'error',
                process_error = $2,
                processed_at = NULL
            FROM aios.dag_node n
            WHERE n.node_id = $1::uuid
              AND ie.event_id = n.event_id
            """,
            payload["node_id"],
            error[:2000],
        )
        return

    if job_type in {"extract_claims", "rdf_liminal_promote"} and payload.get("section_id"):
        await db.execute(
            """
            UPDATE aios.ingest_event ie
            SET process_status = 'error',
                process_error = $2,
                rdf_error = CASE
                    WHEN $3 = 'rdf_liminal_promote' THEN $2
                    ELSE rdf_error
                END,
                processed_at = NULL
            FROM aios.document_section ds
            JOIN aios.dag_node n
              ON n.node_id = ds.node_id
            WHERE ds.section_id = $1::uuid
              AND ie.event_id = n.event_id
            """,
            payload["section_id"],
            error[:2000],
            job_type,
        )


# -------------------------------------------------
# Parallel execution scheduler
# -------------------------------------------------

def _new_database() -> Database:
    return Database(
        settings.db_dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )


async def _resolve_partition_key(db: Database, job: Dict[str, Any]) -> str:
    job_type = str(job["job_type"])
    payload = job.get("payload") or {}
    spec = job_spec(job_type)
    kind = spec.partition_kind

    if kind in {"node_id", "section_id", "claim_id", "character_id", "world_id"}:
        value = payload.get(kind)
        if not value:
            return f"job:{job['job_id']}"
        if kind == "claim_id":
            return f"claim:{value}"
        return f"{kind}:{value}"

    if kind == "claim_scope" and payload.get("claim_id"):
        claim_id = payload["claim_id"]
        row = await db.fetchrow(
            """
            SELECT CASE
                WHEN ccr.epistemic_scope='character'
                     AND ccr.origin_character_id IS NOT NULL
                     AND ccr.character_instance_id IS NOT NULL
                    THEN 'char:' || ccr.origin_character_id
                WHEN ccr.source_id IS NOT NULL
                    THEN 'source:' || ccr.source_id
                WHEN ccr.world_id IS NOT NULL
                    THEN 'world:' || ccr.world_id::text || ':observed'
                ELSE 'claim:' || ccr.claim_id::text
            END AS scope_key
            FROM aios.claim_context_resolution ccr
            WHERE ccr.claim_id=$1::uuid
            """,
            claim_id,
        )
        return str(row["scope_key"]) if row else f"claim:{claim_id}"

    if kind == "acquisition_scope" and payload.get("acquisition_id"):
        acquisition_id = payload["acquisition_id"]
        row = await db.fetchrow(
            """
            SELECT 'char:' || ci.character_id AS scope_key
            FROM aios.knowledge_acquisition_event kae
            JOIN aios.character_instance ci ON ci.instance_id=kae.instance_id
            WHERE kae.acquisition_id=$1::uuid
            """,
            acquisition_id,
        )
        return str(row["scope_key"]) if row else f"acquisition:{acquisition_id}"

    if kind == "assertion_scope" and payload.get("assertion_id"):
        assertion_id = payload["assertion_id"]
        row = await db.fetchrow(
            """
            SELECT 'world:' || world_id::text || ':asserted' AS scope_key
            FROM aios.world_proposition_assertion
            WHERE assertion_id=$1::uuid
            """,
            assertion_id,
        )
        return str(row["scope_key"]) if row else f"assertion:{assertion_id}"

    return f"global:{job_type}"


async def _heartbeat_loop(
    db: Database,
    *,
    job_id: UUID,
    worker_id: str,
) -> None:
    interval = max(5, settings.pipeline_heartbeat_seconds)
    while True:
        await asyncio.sleep(interval)
        alive = await heartbeat_job(
            db,
            job_id=job_id,
            worker_id=worker_id,
            lease_seconds=settings.pipeline_lease_seconds,
        )
        if not alive:
            return


def _run_isolated_handler(job_type: str, job: Dict[str, Any]) -> None:
    """Run blocking/NLP/RDF handlers on a private event loop and DB pool."""

    async def _run() -> None:
        db = _new_database()
        await db.connect()
        try:
            handler = JOB_HANDLERS[job_type]
            await handler(db, job)
        finally:
            await db.close()

    asyncio.run(_run())


async def _execute_claimed_job(
    db: Database,
    *,
    job: Dict[str, Any],
    worker_id: str,
    rdf_gate: asyncio.Semaphore,
) -> None:
    job_id = job["job_id"]
    job_type = str(job["job_type"])
    handler = JOB_HANDLERS.get(job_type)
    if not handler:
        await mark_failed(
            db,
            job_id,
            f"No handler for job_type={job_type}",
            worker_id=worker_id,
        )
        return

    spec = job_spec(job_type)
    partition_key = await _resolve_partition_key(db, job)
    if partition_key.startswith(("char:", "source:", "world:")):
        lock_key = f"semantic-scope:{partition_key}"
    else:
        lock_key = f"{spec.resource_class.value}:{partition_key}"
    lock_started = time.monotonic()

    heartbeat = asyncio.create_task(
        _heartbeat_loop(db, job_id=job_id, worker_id=worker_id)
    )
    try:
        async with db.connection() as lock_conn:
            await lock_conn.execute(
                "SELECT pg_advisory_lock(hashtextextended($1, 0))",
                lock_key,
            )
            lock_wait = time.monotonic() - lock_started
            started = time.monotonic()
            try:
                await db.execute(
                    """
                    UPDATE aios.pipeline_job
                    SET partition_key=$2, updated_at=now()
                    WHERE job_id=$1 AND worker_id=$3
                    """,
                    job_id,
                    partition_key,
                    worker_id,
                )
                async def _invoke_handler() -> None:
                    if spec.isolate_blocking:
                        await asyncio.to_thread(_run_isolated_handler, job_type, job)
                    else:
                        await handler(db, job)

                if spec.requires_rdf_slot:
                    async with rdf_gate:
                        await _invoke_handler()
                else:
                    await _invoke_handler()
                await mark_done(db, job_id, worker_id=worker_id)
                logger.info(
                    "Job done id=%s type=%s resource=%s partition=%s "
                    "lock_wait=%.3fs runtime=%.3fs",
                    job_id,
                    job_type,
                    spec.resource_class.value,
                    partition_key,
                    lock_wait,
                    time.monotonic() - started,
                )
            finally:
                await lock_conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                    lock_key,
                )
    except Exception as exc:
        logger.exception(
            "Job %s (%s) failed resource=%s partition=%s",
            job_id,
            job_type,
            spec.resource_class.value,
            partition_key,
        )
        error = repr(exc)
        try:
            await _mark_origin_event_error(
                db,
                job_type=job_type,
                payload=job.get("payload") or {},
                error=error,
            )
        except Exception:
            logger.exception(
                "Failed to propagate job %s error to originating ingest_event",
                job_id,
            )
        await mark_failed(db, job_id, error, worker_id=worker_id)
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


def _semantic_lane_order(worker_index: int) -> tuple[list[str], list[str]]:
    """Return preferred and fallback lanes for one semantic worker.

    With four default workers this reserves two low-latency lanes for live
    epistemic construction, one for structural work, and one for background
    maintenance. Fallbacks keep the pool fully utilized when a lane is empty.
    """
    live = SchedulingLane.LIVE.value
    structural = SchedulingLane.STRUCTURAL.value
    background = SchedulingLane.BACKGROUND.value
    default = SchedulingLane.DEFAULT.value

    if worker_index in (0, 1):
        return [live], [live, structural, default, background]
    if worker_index == 2:
        return [structural], [structural, live, default, background]
    return [background], [background, structural, live, default]


async def _claim_for_worker(
    db: Database,
    *,
    worker_id: str,
    resource_class: ResourceClass,
    worker_index: int,
    claim_gate: asyncio.Lock,
) -> Optional[Dict[str, Any]]:
    preferred: Optional[list[str]] = None
    fallback: Optional[list[str]] = None
    if resource_class == ResourceClass.SEMANTIC:
        preferred, fallback = _semantic_lane_order(worker_index)

    # Serialize only the short local claim operation. This lets the first
    # worker's queued->running transition become visible before the next local
    # worker selects work, preventing same-partition claim convoys.
    async with claim_gate:
        job = await fetch_next_job(
            db,
            worker_id=worker_id,
            resource_class=resource_class.value,
            lease_seconds=settings.pipeline_lease_seconds,
            scheduling_lanes=preferred,
            prefer_uncontended=True,
        )
        if (
            not job
            and fallback is not None
            and fallback != preferred
        ):
            job = await fetch_next_job(
                db,
                worker_id=worker_id,
                resource_class=resource_class.value,
                lease_seconds=settings.pipeline_lease_seconds,
                scheduling_lanes=fallback,
                prefer_uncontended=True,
            )
        return job


async def _resource_worker(
    db: Database,
    *,
    resource_class: ResourceClass,
    worker_index: int,
    poll_interval: float,
    rdf_gate: asyncio.Semaphore,
    claim_gate: asyncio.Lock,
) -> None:
    worker_id = (
        f"{socket.gethostname()}:{os.getpid()}:"
        f"{resource_class.value}:{worker_index}"
    )
    while True:
        job = await _claim_for_worker(
            db,
            worker_id=worker_id,
            resource_class=resource_class,
            worker_index=worker_index,
            claim_gate=claim_gate,
        )
        if not job:
            await asyncio.sleep(poll_interval)
            continue
        await _execute_claimed_job(
            db,
            job=job,
            worker_id=worker_id,
            rdf_gate=rdf_gate,
        )


async def _scheduler_metrics_loop(db: Database) -> None:
    while True:
        await asyncio.sleep(30)
        rows = await db.fetch(
            """
            SELECT resource_class, scheduling_lane, status, COUNT(*)::integer AS n
            FROM aios.pipeline_job
            WHERE status IN ('queued','running')
            GROUP BY resource_class, scheduling_lane, status
            ORDER BY resource_class, scheduling_lane, status
            """
        )
        summary = ",".join(
            f"{row['resource_class']}/{row['scheduling_lane']}:{row['status']}={row['n']}"
            for row in rows
        ) or "empty"
        logger.info("Scheduler queues %s", summary)


async def _lease_recovery_loop(db: Database) -> None:
    interval = max(15, min(settings.pipeline_lease_seconds // 2, 60))
    while True:
        await asyncio.sleep(interval)
        recovered = await recover_stale_running_jobs(
            db,
            stale_after_seconds=settings.pipeline_stale_running_seconds,
        )
        if recovered:
            logger.warning("Recovered %d expired pipeline worker leases", recovered)


def _worker_limits() -> dict[ResourceClass, int]:
    return {
        ResourceClass.FAST_SQL: max(0, settings.runner_fast_sql_workers),
        ResourceClass.NLP: max(0, settings.runner_nlp_workers),
        ResourceClass.SEMANTIC: max(0, settings.runner_semantic_workers),
        ResourceClass.VECTOR: max(0, settings.runner_vector_workers),
        ResourceClass.RDF: max(0, settings.runner_rdf_workers),
        ResourceClass.RECONCILIATION: max(0, settings.runner_reconciliation_workers),
        ResourceClass.GLOBAL: max(0, settings.runner_global_workers),
    }


async def run_runner(poll_interval: float = 1.0) -> None:
    db = _new_database()
    await db.connect()

    recovered = await recover_stale_running_jobs(
        db,
        stale_after_seconds=settings.pipeline_stale_running_seconds,
    )
    if recovered:
        logger.warning("Recovered %d stale/expired pipeline jobs", recovered)

    rebalanced = await rebalance_queued_priorities(db)
    if rebalanced:
        logger.info("Rebalanced priorities for %d queued pipeline jobs", rebalanced)

    limits = _worker_limits()
    logger.info(
        "Pipeline runner started scheduler=%s",
        ",".join(f"{resource.value}:{count}" for resource, count in limits.items()),
    )

    rdf_gate = asyncio.Semaphore(max(1, settings.runner_rdf_workers))
    claim_gate = asyncio.Lock()
    tasks: list[asyncio.Task] = [
        asyncio.create_task(_lease_recovery_loop(db), name="lease-recovery"),
        asyncio.create_task(_scheduler_metrics_loop(db), name="scheduler-metrics"),
    ]
    for resource_class, count in limits.items():
        for worker_index in range(count):
            tasks.append(
                asyncio.create_task(
                    _resource_worker(
                        db,
                        resource_class=resource_class,
                        worker_index=worker_index,
                        poll_interval=poll_interval,
                        rdf_gate=rdf_gate,
                        claim_gate=claim_gate,
                    ),
                    name=f"{resource_class.value}-{worker_index}",
                )
            )

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_runner(poll_interval=settings.runner_poll_interval))
