# aios_app/supervisor.py

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List

from aios_app.config import settings
from aios_app.db import Database
from aios_app.pipeline.jobs import enqueue_job

logger = logging.getLogger("aios.supervisor")


# =================================================
# Stage definition
# =================================================

@dataclass(frozen=True)
class Stage:
    """
    Declarative description of a pipeline stage.

    eligibility_sql MUST exclude already queued/running jobs.
    Payload is passed verbatim to the runner.
    """
    name: str
    job_type: str
    eligibility_sql: str
    payload_builder: Callable[[Dict[str, object]], Dict[str, object]]


# =================================================
# Payload builders
# =================================================

def node_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"node_id": str(row["node_id"])}


def section_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"section_id": str(row["section_id"])}


def claim_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"claim_id": str(row["claim_id"])}


def character_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"character_id": str(row["character_id"])}


def empty_payload(_: Dict[str, object]) -> Dict[str, object]:
    return {}


# =================================================
# Pipeline stages
# =================================================

STAGES: List[Stage] = [

    # -------------------------------------------------
    # 0) ingest_event -> character_identity
    # -------------------------------------------------
    # Character discovery must happen independently of claim/RDF processing so
    # runtime activation can resolve a newly observed character as soon as the
    # transport has durably ingested any event for that character.
    Stage(
        name="discover_characters",
        job_type="discover_characters",
        eligibility_sql="""
        SELECT DISTINCT ie.character_id
        FROM aios.ingest_event ie
        WHERE ie.character_id IS NOT NULL
          AND btrim(ie.character_id) <> ''
          AND NOT EXISTS (
              SELECT 1
              FROM aios.character_identity ci
              WHERE ci.character_id = ie.character_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type = 'discover_characters'
                AND pj.status IN ('queued', 'running')
                AND pj.payload->>'character_id' = ie.character_id
          )
        ORDER BY ie.character_id
        LIMIT $1
        """,
        payload_builder=character_id_payload,
    ),

    # -------------------------------------------------
    # 1) DAG → document_section
    # -------------------------------------------------
    Stage(
        name="dag_to_document_section",
        job_type="dag_to_document_section",
        eligibility_sql="""
        SELECT n.node_id
        FROM aios.dag_node n
        WHERE n.message_text IS NOT NULL
          AND (
              (
                  n.kind = 'paragraph'
                  AND n.payload ? 'document_id'
                  AND n.payload ? 'paragraph_index'
              )
              OR
              (
                  n.kind = 'chat_message'
                  AND n.event_id IS NOT NULL
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aios.document_section ds
              WHERE ds.node_id = n.node_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type = 'dag_to_document_section'
                AND pj.status IN ('queued', 'running')
                AND (pj.payload->>'node_id') = n.node_id::text
          )
        ORDER BY n.event_id
        LIMIT $1
        """,
        payload_builder=node_id_payload,
    ),

    # -------------------------------------------------
    # 2) document_section → claim_candidate
    # -------------------------------------------------
    Stage(
        name="extract_claims",
        job_type="extract_claims",
        eligibility_sql="""
        SELECT ds.section_id
        FROM aios.document_section ds
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        WHERE ds.claims_extracted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type = 'extract_claims'
                AND pj.status IN ('queued', 'running')
                AND (pj.payload->>'section_id') = ds.section_id::text
          )
        ORDER BY n.event_id
        LIMIT $1
        """,
        payload_builder=section_id_payload,
    ),

    # -------------------------------------------------
    # 3) claim_candidate -> normalized proposition/observation
    # -------------------------------------------------
    Stage(
        name="normalize_proposition",
        job_type="normalize_proposition",
        eligibility_sql="""
        SELECT cc.claim_id
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es
          ON es.sentence_id = cc.sentence_id
        JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        WHERE EXISTS (
            SELECT 1
            FROM aios.claim_context_resolution ccr
            WHERE ccr.claim_id=cc.claim_id
        )
          AND NOT EXISTS (
            SELECT 1 FROM aios.observation o WHERE o.claim_id=cc.claim_id
        )
          AND NOT EXISTS (
            SELECT 1 FROM aios.pipeline_job pj
            WHERE pj.job_type='normalize_proposition'
              AND pj.status IN ('queued','running')
              AND pj.payload->>'claim_id'=cc.claim_id::text
          )
        ORDER BY cc.created_at
        LIMIT $1
        """,
        payload_builder=claim_id_payload,
    ),

    # -------------------------------------------------
    # 4) normalized observation -> RDF epistemic graph
    # -------------------------------------------------
    Stage(
        name="rdf_epistemic_project",
        job_type="rdf_epistemic_project",
        eligibility_sql="""
        SELECT o.claim_id
        FROM aios.observation o
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.rdf_promotion_log rpl
            WHERE rpl.claim_id=o.claim_id
              AND rpl.rdf_dataset='world'
              AND rpl.rdf_graph='urn:aios:world:epistemic'
              AND rpl.rdf_predicate='world:observesProposition'
        )
          AND NOT EXISTS (
            SELECT 1 FROM aios.pipeline_job pj
            WHERE pj.job_type='rdf_epistemic_project'
              AND pj.status IN ('queued','running')
              AND pj.payload->>'claim_id'=o.claim_id::text
          )
        ORDER BY o.observed_at
        LIMIT $1
        """,
        payload_builder=claim_id_payload,
    ),

    # -------------------------------------------------
    # 4) observations -> source narrative clusters
    # -------------------------------------------------
    Stage(
        name="assign_narratives",
        job_type="assign_narratives",
        eligibility_sql="""
        SELECT 1
        WHERE EXISTS (
            SELECT 1
            FROM aios.observation o
            WHERE NOT EXISTS (
                SELECT 1 FROM aios.narrative_membership nm
                WHERE nm.observation_id=o.observation_id
            )
        )
          AND NOT EXISTS (
            SELECT 1 FROM aios.pipeline_job pj
            WHERE pj.job_type='assign_narratives'
              AND pj.status IN ('queued','running')
          )
        LIMIT $1
        """,
        payload_builder=empty_payload,
    ),

    # -------------------------------------------------
    # 5) explicit acquisition events -> character knowledge
    # -------------------------------------------------
    Stage(
        name="project_character_knowledge",
        job_type="project_character_knowledge",
        eligibility_sql="""
        SELECT 1
        WHERE EXISTS (
            SELECT 1 FROM aios.knowledge_acquisition_event
            WHERE processed_at IS NULL
        )
          AND NOT EXISTS (
            SELECT 1 FROM aios.pipeline_job pj
            WHERE pj.job_type='project_character_knowledge'
              AND pj.status IN ('queued','running')
          )
        LIMIT $1
        """,
        payload_builder=empty_payload,
    ),

    # -------------------------------------------------
    # 6) provisional generated world facts -> reconciliation
    # -------------------------------------------------
    Stage(
        name="resolve_generated_facts",
        job_type="resolve_generated_facts",
        eligibility_sql="""
        SELECT 1
        WHERE EXISTS (
            SELECT 1
            FROM aios.world_proposition_assertion a
            JOIN aios.proposition p ON p.proposition_id=a.proposition_id
            WHERE a.source_kind='generated_fill'
              AND a.epistemic_status='provisional'
              AND (
                  a.last_checked_at IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM aios.observation o
                      JOIN aios.timeline t ON t.timeline_id=o.timeline_id
                      JOIN aios.proposition op ON op.proposition_id=o.proposition_id
                      WHERE t.world_id=a.world_id
                        AND op.topic_key=p.topic_key
                        AND o.observed_at > a.last_checked_at
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM aios.world_proposition_assertion wa
                      JOIN aios.proposition wp ON wp.proposition_id=wa.proposition_id
                      WHERE wa.world_id=a.world_id
                        AND wa.source_kind='observed'
                        AND wa.epistemic_status NOT IN ('rejected','superseded')
                        AND wp.topic_key=p.topic_key
                        AND wa.updated_at > a.last_checked_at
                  )
              )
        )
          AND NOT EXISTS (
            SELECT 1 FROM aios.pipeline_job pj
            WHERE pj.job_type='resolve_generated_facts'
              AND pj.status IN ('queued','running')
          )
        LIMIT $1
        """,
        payload_builder=empty_payload,
    ),

    # -------------------------------------------------
    # 3) section claim set → RDF /world/liminal
    # -------------------------------------------------
    # One job owns one section. This preserves the lineage boundary:
    # ingest_event → dag_node → document_section → claims → RDF receipts.
    Stage(
        name="rdf_liminal_promote",
        job_type="rdf_liminal_promote",
        eligibility_sql="""
        SELECT ds.section_id
        FROM aios.document_section ds
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        JOIN aios.ingest_event ie
          ON ie.event_id = n.event_id
        WHERE ds.claims_extracted_at IS NOT NULL
          AND ie.rdf_processed_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type = 'rdf_liminal_promote'
                AND (pj.payload->>'section_id') = ds.section_id::text
                AND (
                    pj.status IN ('queued', 'running')
                    OR (
                        pj.status = 'failed'
                        AND pj.updated_at > now() - interval '30 seconds'
                    )
                )
          )
        ORDER BY n.event_id
        LIMIT $1
        """,
        payload_builder=section_id_payload,
    ),

    # -------------------------------------------------
    # 4) classify promoted liminal RDF claims
    # -------------------------------------------------
    # The old gate stopped scheduling forever after ANY contentKind receipt
    # existed. Keep scheduling while at least one base-promoted claim lacks its
    # own classification receipt.
    Stage(
        name="rdf_liminal_classify",
        job_type="rdf_liminal_classify",
        eligibility_sql="""
        SELECT 1
        WHERE EXISTS (
            SELECT 1
            FROM aios.claim_candidate cc
            WHERE EXISTS (
                SELECT 1
                FROM aios.rdf_promotion_log base
                WHERE base.claim_id = cc.claim_id
                  AND base.rdf_dataset = 'world'
                  AND base.rdf_graph = 'urn:aios:world:liminal'
                  AND base.rdf_predicate = 'rdf:type'
                  AND base.rdf_object = 'world:Claim'
            )
              AND NOT EXISTS (
                  SELECT 1
                  FROM aios.rdf_promotion_log cls
                  WHERE cls.claim_id = cc.claim_id
                    AND cls.rdf_dataset = 'world'
                    AND cls.rdf_graph = 'urn:aios:world:liminal'
                    AND cls.rdf_predicate = 'world:contentKind'
              )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj
            WHERE pj.job_type = 'rdf_liminal_classify'
              AND pj.status IN ('queued', 'running')
        )
        LIMIT $1
        """,
        payload_builder=empty_payload,
    ),

    # -------------------------------------------------
    # 5) liminal claim -> durable context resolution
    # -------------------------------------------------
    # Context is derived from trusted DAG/timeline lineage first, then semantic
    # classification annotates the claim. It never promotes the proposition to
    # world truth and never grants it to another character.
    Stage(
        name="resolve_claim_context",
        job_type="resolve_claim_context",
        eligibility_sql="""
        SELECT cc.claim_id
        FROM aios.claim_candidate cc
        WHERE EXISTS (
            SELECT 1
            FROM aios.rdf_promotion_log base
            WHERE base.claim_id=cc.claim_id
              AND base.rdf_dataset='world'
              AND base.rdf_graph='urn:aios:world:liminal'
              AND base.rdf_predicate='rdf:type'
              AND base.rdf_object='world:Claim'
        )
          AND EXISTS (
            SELECT 1
            FROM aios.rdf_promotion_log cls
            WHERE cls.claim_id=cc.claim_id
              AND cls.rdf_dataset='world'
              AND cls.rdf_graph='urn:aios:world:liminal'
              AND cls.rdf_predicate='world:contentKind'
        )
          AND NOT EXISTS (
            SELECT 1
            FROM aios.claim_context_resolution ccr
            WHERE ccr.claim_id=cc.claim_id
        )
          AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj
            WHERE pj.job_type='resolve_claim_context'
              AND pj.status IN ('queued','running')
              AND pj.payload->>'claim_id'=cc.claim_id::text
        )
        ORDER BY cc.created_at
        LIMIT $1
        """,
        payload_builder=claim_id_payload,
    ),
]


# =================================================
# Backpressure helpers
# =================================================

async def queued_job_count(db: Database) -> int:
    row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM aios.pipeline_job WHERE status = 'queued'"
    )
    return int(row["cnt"])


# =================================================
# Supervisor internals
# =================================================

async def enqueue_stage_jobs(
    db: Database,
    stage: Stage,
    *,
    batch_size: int,
) -> int:
    rows: Iterable[Dict[str, object]] = await db.fetch(stage.eligibility_sql, batch_size)

    count = 0
    for row in rows:
        payload = stage.payload_builder(dict(row))
        await enqueue_job(db, job_type=stage.job_type, payload=payload)
        count += 1

    return count


# =================================================
# Supervisor loop
# =================================================

async def run_supervisor() -> None:
    poll_interval = settings.supervisor_poll_interval
    batch_size = settings.supervisor_batch_size
    max_jobs_per_cycle = settings.supervisor_max_jobs_per_cycle
    max_queued_backlog = getattr(settings, "supervisor_max_queued_backlog", 500)

    db = Database(settings.db_dsn)
    await db.connect()

    logger.info("AIOS supervisor started")

    try:
        while True:
            qcnt = await queued_job_count(db)
            if qcnt >= max_queued_backlog:
                await asyncio.sleep(poll_interval)
                continue

            remaining = max_jobs_per_cycle
            scheduled = 0

            for stage in STAGES:
                if remaining <= 0:
                    break

                try:
                    n = await enqueue_stage_jobs(
                        db,
                        stage,
                        batch_size=min(batch_size, remaining),
                    )
                    scheduled += n
                    remaining -= n
                except Exception:
                    logger.exception("Stage '%s' enqueue failed", stage.name)

            if scheduled == 0:
                await asyncio.sleep(poll_interval)

    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervisor())
