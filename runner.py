# aios_app/runner.py

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable, Dict, Any
from uuid import UUID

from aios_app.config import settings
from aios_app.db import Database
from aios_app.pipeline.jobs import (
    fetch_next_job,
    mark_done,
    mark_failed,
)

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
# Runner loop
# -------------------------------------------------

async def run_runner(poll_interval: float = 1.0) -> None:
    db = Database(settings.db_dsn)
    await db.connect()

    logger.info("Pipeline runner started")

    try:
        while True:
            # fetch_next_job() atomically moves queued → running, so there is no
            # lock gap between selecting and claiming a job.
            job = await fetch_next_job(db)
            if not job:
                await asyncio.sleep(poll_interval)
                continue

            job_id = job["job_id"]
            job_type = job["job_type"]
            handler = JOB_HANDLERS.get(job_type)

            if not handler:
                await mark_failed(db, job_id, f"No handler for job_type={job_type}")
                continue

            try:
                await handler(db, job)
                await mark_done(db, job_id)
            except Exception as exc:
                logger.exception("Job %s (%s) failed", job_id, job_type)
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
                await mark_failed(db, job_id, error)

    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_runner(poll_interval=settings.runner_poll_interval))
