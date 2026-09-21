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
CURRENT_RESOLVER_VERSION = "context-resolver-v4-semantic"


@dataclass(frozen=True)
class Stage:
    name: str
    job_type: str
    eligibility_sql: str
    payload_builder: Callable[[Dict[str, object]], Dict[str, object]]
    priority: int = 100
    queue_limit: int = 64
    critical: bool = False


def node_id_payload(row): return {"node_id": str(row["node_id"])}
def section_id_payload(row): return {"section_id": str(row["section_id"])}
def claim_id_payload(row): return {"claim_id": str(row["claim_id"])}
def resolver_claim_payload(row): return {"claim_id": str(row["claim_id"]), "admission_band": str(row.get("admission_band") or "backlog")}
def semantic_backfill_claim_payload(row): return {"claim_id": str(row["claim_id"]), "semantic_backfill": "proposition_leaves_20260909"}
def character_id_payload(row): return {"character_id": str(row["character_id"])}
def world_id_payload(row): return {"world_id": str(row["world_id"])}
def assertion_id_payload(row): return {"assertion_id": str(row["assertion_id"])}
def acquisition_id_payload(row): return {"acquisition_id": str(row["acquisition_id"])}
def empty_payload(_): return {}

# Expansion stages use this contract after normalization:
# - exact reinforcement: already completed by suppression receipts
# - exact novel: wait for a terminal vector admission decision
# - vector reinforcement: completed by suppression receipts
# - novel/refine/challenge/bypass: expand normally
ADMISSION_EXPANSION_SQL = """
          AND EXISTS (
              SELECT 1
              FROM aios.semantic_exact_admission sea
              WHERE sea.claim_id=o.claim_id
                AND (
                    sea.decision='reinforces_exact'
                    OR (
                        sea.decision='novel_exact'
                        AND EXISTS (
                            SELECT 1 FROM aios.semantic_neighbor_admission sna
                            WHERE sna.claim_id=o.claim_id
                              AND sna.decision IN (
                                  'reinforces','refines','challenges','novel',
                                  'bypass_unavailable','bypass_timeout','bypass_error'
                              )
                        )
                    )
                )
          )
"""


def _admission(sql: str) -> str:
    return sql.replace("/* ADMISSION_BARRIER */", ADMISSION_EXPANSION_SQL)


STAGES: List[Stage] = [
    Stage("discover_characters", "discover_characters", """
        SELECT DISTINCT ie.character_id FROM aios.ingest_event ie
        WHERE ie.character_id IS NOT NULL AND btrim(ie.character_id)<>''
          AND NOT EXISTS (SELECT 1 FROM aios.character_identity ci WHERE ci.character_id=ie.character_id)
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='discover_characters' AND pj.status IN ('queued','running'))
        ORDER BY ie.character_id LIMIT LEAST($1,1)
    """, character_id_payload, 10, 8, True),
    Stage("project_world_topology", "project_world_topology", """
        SELECT w.world_id FROM aios.world w LEFT JOIN aios.world_rdf_projection wrp ON wrp.world_id=w.world_id
        WHERE (wrp.world_id IS NULL OR wrp.projected_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='project_world_topology' AND (pj.status IN ('queued','running') OR (pj.status='failed' AND pj.updated_at>now()-interval '30 seconds')))
        ORDER BY w.created_at LIMIT LEAST($1,1)
    """, world_id_payload, 20, 8, True),
    Stage("dag_to_document_section", "dag_to_document_section", """
        SELECT n.node_id FROM aios.dag_node n JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        WHERE n.message_text IS NOT NULL AND ie.superseded_at IS NULL
          AND (((n.kind='paragraph') AND n.payload?'document_id' AND n.payload?'paragraph_index') OR (n.kind IN ('chat_message','observation') AND n.event_id IS NOT NULL))
          AND NOT EXISTS (SELECT 1 FROM aios.document_section ds WHERE ds.node_id=n.node_id)
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='dag_to_document_section' AND pj.status IN ('queued','running') AND pj.payload->>'node_id'=n.node_id::text)
        ORDER BY n.event_id LIMIT $1
    """, node_id_payload, 20, 48, True),
    Stage("extract_claims", "extract_claims", """
        SELECT ds.section_id FROM aios.document_section ds JOIN aios.dag_node n ON n.node_id=ds.node_id JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        WHERE ds.claims_extracted_at IS NULL AND ie.superseded_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='extract_claims' AND pj.status IN ('queued','running') AND pj.payload->>'section_id'=ds.section_id::text)
        ORDER BY n.event_id LIMIT $1
    """, section_id_payload, 25, 48, True),
    Stage("decompose_claim_frames", "decompose_claim_frames", """
        SELECT cc.claim_id FROM aios.claim_candidate cc JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id JOIN aios.document_section ds ON ds.section_id=es.section_id JOIN aios.dag_node dn ON dn.node_id=ds.node_id JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE ie.superseded_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM aios.claim_semantic_frame_projection sfp WHERE sfp.claim_id=cc.claim_id AND sfp.decomposer_version='semantic-frame-v2')
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='decompose_claim_frames' AND pj.status IN ('queued','running') AND pj.payload->>'claim_id'=cc.claim_id::text)
        ORDER BY cc.created_at LIMIT $1
    """, claim_id_payload, 27, 128, True),
    Stage("resolve_claim_context", "resolve_claim_context", """
        WITH eligible AS (
          SELECT cc.claim_id,cc.created_at FROM aios.claim_candidate cc
          WHERE EXISTS (SELECT 1 FROM aios.claim_semantic_frame_projection sfp WHERE sfp.claim_id=cc.claim_id AND sfp.decomposer_version='semantic-frame-v2')
            AND NOT EXISTS (SELECT 1 FROM aios.claim_context_resolution ccr WHERE ccr.claim_id=cc.claim_id AND ccr.resolver_version='context-resolver-v4-semantic')
            AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='resolve_claim_context' AND pj.status IN ('queued','running') AND pj.payload->>'claim_id'=cc.claim_id::text)
        ), quotas AS (SELECT floor($1::numeric*0.75)::integer backlog_quota,$1-floor($1::numeric*0.75)::integer fresh_quota),
        backlog AS (SELECT e.claim_id,e.created_at,'backlog'::text admission_band FROM eligible e ORDER BY e.created_at,e.claim_id LIMIT (SELECT backlog_quota FROM quotas)),
        fresh AS (SELECT e.claim_id,e.created_at,'fresh'::text admission_band FROM eligible e WHERE NOT EXISTS(SELECT 1 FROM backlog b WHERE b.claim_id=e.claim_id) ORDER BY e.created_at DESC,e.claim_id LIMIT (SELECT fresh_quota FROM quotas))
        SELECT claim_id,admission_band FROM (SELECT claim_id,created_at,admission_band,0 band_order FROM backlog UNION ALL SELECT claim_id,created_at,admission_band,1 FROM fresh) s ORDER BY band_order,created_at LIMIT $1
    """, resolver_claim_payload, 30, 256, True),
    Stage("normalize_proposition", "normalize_proposition", """
        SELECT cc.claim_id FROM aios.claim_candidate cc JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id JOIN aios.document_section ds ON ds.section_id=es.section_id JOIN aios.dag_node n ON n.node_id=ds.node_id JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        WHERE ie.superseded_at IS NULL
          AND EXISTS (SELECT 1 FROM aios.claim_context_resolution ccr WHERE ccr.claim_id=cc.claim_id AND ccr.resolver_version='context-resolver-v4-semantic')
          AND EXISTS (SELECT 1 FROM aios.claim_semantic_frame_projection sfp WHERE sfp.claim_id=cc.claim_id AND sfp.decomposer_version='semantic-frame-v2')
          AND (NOT EXISTS(SELECT 1 FROM aios.observation o WHERE o.claim_id=cc.claim_id) OR EXISTS(SELECT 1 FROM aios.observation o JOIN aios.claim_semantic_frame sf ON sf.claim_id=o.claim_id AND sf.decomposer_version='semantic-frame-v2' LEFT JOIN aios.observation_proposition op ON op.observation_id=o.observation_id AND op.frame_id=sf.frame_id WHERE o.claim_id=cc.claim_id AND op.frame_id IS NULL))
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='normalize_proposition' AND pj.status IN ('queued','running') AND pj.payload->>'claim_id'=cc.claim_id::text)
        ORDER BY cc.created_at LIMIT $1
    """, claim_id_payload, 35, 96, True),
    Stage("materialize_event_occurrences", "materialize_event_occurrences", """
        SELECT DISTINCT o.claim_id
        FROM aios.observation o
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
        WHERE upper(COALESCE(ccr.claim_kind,''))='EVENT'
          AND NOT EXISTS (
              SELECT 1 FROM aios.semantic_event_membership sem
              WHERE sem.observation_id=o.observation_id AND sem.status='active'
          )
          AND NOT EXISTS (
              SELECT 1 FROM aios.pipeline_job pj
              WHERE pj.job_type='materialize_event_occurrences'
                AND pj.status IN ('queued','running')
                AND pj.payload->>'claim_id'=o.claim_id::text
          )
        ORDER BY o.claim_id LIMIT $1
    """, claim_id_payload, 38, 96, True),
    Stage("project_character_knowledge", "project_character_knowledge", """
        SELECT 1 WHERE EXISTS (SELECT 1 FROM aios.knowledge_acquisition_event kae LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id WHERE kae.processed_at IS NULL AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL))
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='project_character_knowledge' AND pj.status IN ('queued','running')) LIMIT $1
    """, empty_payload, 40, 4, True),
    Stage("derive_character_acquisition_topology", "derive_character_acquisition_topology", """
        SELECT kae.acquisition_id FROM aios.knowledge_acquisition_event kae LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE kae.proposition_id IS NOT NULL AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM aios.semantic_topology_projection stp WHERE stp.acquisition_id=kae.acquisition_id AND stp.projected_at IS NOT NULL AND stp.resolver_version='semantic-topology-v1')
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='derive_character_acquisition_topology' AND pj.status IN ('queued','running') AND pj.payload->>'acquisition_id'=kae.acquisition_id::text)
        ORDER BY kae.created_at LIMIT $1
    """, acquisition_id_payload, 45, 48, True),
    Stage("derive_world_assertion_topology", "derive_world_assertion_topology", """
        SELECT a.assertion_id FROM aios.world_proposition_assertion a WHERE a.epistemic_status NOT IN ('rejected','superseded')
          AND NOT EXISTS (SELECT 1 FROM aios.semantic_topology_projection stp WHERE stp.assertion_id=a.assertion_id AND stp.projected_at IS NOT NULL AND stp.resolver_version='semantic-topology-v1')
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='derive_world_assertion_topology' AND pj.status IN ('queued','running') AND pj.payload->>'assertion_id'=a.assertion_id::text)
        ORDER BY a.created_at LIMIT $1
    """, assertion_id_payload, 50, 32, True),
    Stage("resolve_generated_facts", "resolve_generated_facts", """
        SELECT 1 WHERE EXISTS (SELECT 1 FROM aios.world_proposition_assertion a WHERE a.source_kind='generated_fill' AND a.epistemic_status='provisional')
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='resolve_generated_facts' AND pj.status IN ('queued','running')) LIMIT $1
    """, empty_payload, 60, 2),
    Stage("rdf_liminal_promote", "rdf_liminal_promote", """
        SELECT ds.section_id FROM aios.document_section ds JOIN aios.dag_node n ON n.node_id=ds.node_id JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        WHERE ds.claims_extracted_at IS NOT NULL AND ie.superseded_at IS NULL AND ie.rdf_processed_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='rdf_liminal_promote' AND pj.payload->>'section_id'=ds.section_id::text AND (pj.status IN ('queued','running') OR (pj.status='failed' AND pj.updated_at>now()-interval '30 seconds')))
        ORDER BY n.event_id LIMIT $1
    """, section_id_payload, 70, 48),
    Stage("rdf_liminal_classify", "rdf_liminal_classify", """
        SELECT 1 WHERE EXISTS (SELECT 1 FROM aios.claim_candidate cc WHERE EXISTS(SELECT 1 FROM aios.rdf_promotion_log base WHERE base.claim_id=cc.claim_id AND base.rdf_dataset='world' AND base.rdf_graph='urn:aios:world:liminal' AND base.rdf_predicate='rdf:type' AND base.rdf_object='world:Claim') AND NOT EXISTS(SELECT 1 FROM aios.rdf_promotion_log cls WHERE cls.claim_id=cc.claim_id AND cls.rdf_dataset='world' AND cls.rdf_graph='urn:aios:world:liminal' AND cls.rdf_predicate='world:contentKind'))
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='rdf_liminal_classify' AND pj.status IN ('queued','running')) LIMIT $1
    """, empty_payload, 75, 1),
    Stage("rdf_epistemic_project", "rdf_epistemic_project", _admission("""
        SELECT o.claim_id FROM aios.observation o JOIN aios.claim_candidate cc ON cc.claim_id=o.claim_id JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id JOIN aios.document_section ds ON ds.section_id=es.section_id JOIN aios.dag_node dn ON dn.node_id=ds.node_id JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE ie.superseded_at IS NULL
          /* ADMISSION_BARRIER */
          AND NOT EXISTS (SELECT 1 FROM aios.rdf_promotion_log rpl WHERE rpl.claim_id=o.claim_id AND rpl.rdf_dataset='world' AND rpl.rdf_graph='urn:aios:world:epistemic' AND rpl.rdf_predicate='world:observesProposition')
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='rdf_epistemic_project' AND pj.status IN ('queued','running') AND pj.payload->>'claim_id'=o.claim_id::text)
        ORDER BY o.observed_at LIMIT $1
    """), claim_id_payload, 80, 64),
    Stage("backfill_semantic_proposition_leaves", "derive_claim_topology", """
        SELECT DISTINCT o.claim_id FROM aios.observation o JOIN aios.semantic_topology_projection stp ON stp.claim_id=o.claim_id JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id JOIN aios.claim_candidate cc ON cc.claim_id=o.claim_id JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id JOIN aios.document_section ds ON ds.section_id=es.section_id JOIN aios.dag_node dn ON dn.node_id=ds.node_id JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE ie.superseded_at IS NULL AND stp.projected_at IS NULL AND stp.resolver_version='semantic-topology-v1' AND stp.meta->>'reproject_reason'='semantic_proposition_leaves_20260909'
          AND NOT EXISTS (SELECT 1 FROM aios.semantic_topology_node n WHERE n.scope_key=stp.scope_key AND n.node_type='PROPOSITION' AND n.proposition_id=o.proposition_id)
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='derive_claim_topology' AND pj.status IN ('queued','running') AND pj.payload->>'claim_id'=o.claim_id::text)
        ORDER BY o.claim_id LIMIT $1
    """, semantic_backfill_claim_payload, 85, 96),
    Stage("derive_claim_topology", "derive_claim_topology", _admission("""
        SELECT o.claim_id FROM aios.observation o JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id JOIN aios.claim_candidate cc ON cc.claim_id=o.claim_id JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id JOIN aios.document_section ds ON ds.section_id=es.section_id JOIN aios.dag_node dn ON dn.node_id=ds.node_id JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE ie.superseded_at IS NULL
          /* ADMISSION_BARRIER */
          AND NOT EXISTS (SELECT 1 FROM aios.semantic_topology_projection stp WHERE stp.claim_id=o.claim_id AND stp.projected_at IS NOT NULL AND stp.resolver_version='semantic-topology-v1')
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='derive_claim_topology' AND pj.status IN ('queued','running') AND pj.payload->>'claim_id'=o.claim_id::text)
        ORDER BY o.observed_at LIMIT $1
    """), claim_id_payload, 90, 64),
    Stage("derive_semantic_episodes", "derive_semantic_episodes", """
        SELECT 1
        WHERE EXISTS (
            SELECT 1 FROM aios.semantic_event se
            WHERE se.status='active' AND se.dag_node_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM aios.semantic_episode_membership em
                  WHERE em.semantic_event_id=se.semantic_event_id AND em.status='active'
              )
        )
          AND NOT EXISTS (
              SELECT 1 FROM aios.pipeline_job pj
              WHERE pj.job_type='derive_semantic_episodes'
                AND pj.status IN ('queued','running')
          )
        LIMIT $1
    """, empty_payload, 92, 1),
    Stage("assign_narratives", "assign_narratives", """
        SELECT 1 WHERE EXISTS (SELECT 1 FROM aios.observation o WHERE NOT EXISTS(SELECT 1 FROM aios.narrative_membership nm WHERE nm.observation_id=o.observation_id))
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='assign_narratives' AND pj.status IN ('queued','running')) LIMIT $1
    """, empty_payload, 95, 1),
]


async def queued_job_count(db: Database) -> int:
    row = await db.fetchrow("SELECT COUNT(*) AS cnt FROM aios.pipeline_job WHERE status='queued'")
    return int(row["cnt"])


async def queued_job_counts_by_type(db: Database) -> dict[str, int]:
    rows = await db.fetch("SELECT job_type,COUNT(*) AS cnt FROM aios.pipeline_job WHERE status='queued' GROUP BY job_type")
    return {str(row["job_type"]): int(row["cnt"]) for row in rows}


def stage_admission_capacity(*, stage: Stage, total_queued: int, stage_queued: int,
                             batch_size: int, remaining_cycle: int,
                             soft_cap: int, critical_reserve: int) -> int:
    stage_capacity = max(0, stage.queue_limit-stage_queued)
    if stage_capacity <= 0 or remaining_cycle <= 0: return 0
    hard_cap = soft_cap+critical_reserve
    global_capacity = max(0, hard_cap-total_queued) if stage.critical else max(0, soft_cap-total_queued)
    if global_capacity <= 0: return 0
    return min(batch_size, remaining_cycle, stage_capacity, global_capacity)


async def enqueue_stage_jobs(db: Database, stage: Stage, *, batch_size: int) -> int:
    rows: Iterable[Dict[str, object]] = await db.fetch(stage.eligibility_sql, batch_size)
    count = 0
    for row in rows:
        payload = stage.payload_builder(dict(row))
        job_id = await enqueue_job(db, job_type=stage.job_type, payload=payload, priority=stage.priority)
        if job_id is not None: count += 1
    return count


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
            queued_by_type = await queued_job_counts_by_type(db)
            critical_reserve = getattr(settings, "supervisor_critical_queue_reserve", 128)
            remaining = max_jobs_per_cycle
            scheduled = 0
            for stage in sorted(STAGES, key=lambda value: value.priority):
                if remaining <= 0: break
                stage_queued = queued_by_type.get(stage.job_type, 0)
                allowed = stage_admission_capacity(stage=stage,total_queued=qcnt,stage_queued=stage_queued,batch_size=batch_size,remaining_cycle=remaining,soft_cap=max_queued_backlog,critical_reserve=critical_reserve)
                if allowed <= 0: continue
                try:
                    n = await enqueue_stage_jobs(db, stage, batch_size=allowed)
                    scheduled += n; remaining -= n; qcnt += n
                    queued_by_type[stage.job_type] = stage_queued+n
                except Exception:
                    logger.exception("Stage '%s' enqueue failed", stage.name)
            if scheduled == 0: await asyncio.sleep(poll_interval)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervisor())
