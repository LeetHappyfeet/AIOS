from __future__ import annotations

import asyncio
import logging
import time
import asyncpg
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
def live_instance_payload(row): return {"live_instance_id": str(row["live_instance_id"])}
def empty_payload(_): return {}

# Expansion stages use this contract after normalization:
# - exact reinforcement: already completed by suppression receipts
# - exact novel: wait for a terminal vector admission decision
# - vector reinforcement: completed by suppression receipts
# - novel/refine/challenge: expand only with standalone occurrence support
# - timeout/error/unavailable bypass: preserve evidence, retry admission
ADMISSION_EXPANSION_SQL = """
          AND aios.semantic_claim_topology_eligible(o.claim_id)
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
                                  'reinforces','refines','challenges','novel'
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
        WITH eligible AS (
        SELECT n.node_id,n.event_id,pfl.updated_at AS foreground_at
        FROM aios.dag_node n JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        LEFT JOIN LATERAL (
          SELECT updated_at FROM aios.pipeline_foreground_lineage pfl
          WHERE pfl.timeline_id=n.timeline_id
            AND n.event_id BETWEEN pfl.first_event_id AND pfl.head_event_id
            AND pfl.expires_at>now()
          ORDER BY updated_at DESC LIMIT 1
        ) pfl ON true
        WHERE n.message_text IS NOT NULL AND ie.superseded_at IS NULL
          AND (((n.kind='paragraph') AND n.payload?'document_id' AND n.payload?'paragraph_index') OR (n.kind IN ('chat_message','observation') AND n.event_id IS NOT NULL))
          AND NOT EXISTS (SELECT 1 FROM aios.document_section ds WHERE ds.node_id=n.node_id)
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='dag_to_document_section' AND pj.status IN ('queued','running') AND pj.payload->>'node_id'=n.node_id::text)
        ), foreground AS (
          SELECT node_id,0 AS band FROM eligible WHERE foreground_at IS NOT NULL
          ORDER BY foreground_at DESC,event_id DESC
          LIMIT GREATEST(1,ceil($1::numeric*0.75)::integer)
        ), backlog AS (
          SELECT e.node_id,1 AS band FROM eligible e
          WHERE NOT EXISTS (SELECT 1 FROM foreground f WHERE f.node_id=e.node_id)
          ORDER BY e.event_id
          LIMIT $1-(SELECT count(*) FROM foreground)
        )
        SELECT node_id FROM (SELECT * FROM foreground UNION ALL SELECT * FROM backlog) admitted
        ORDER BY band
    """, node_id_payload, 20, 48, True),
    Stage("extract_claims", "extract_claims", """
        WITH eligible AS (
        SELECT ds.section_id,n.event_id,pfl.updated_at AS foreground_at
        FROM aios.document_section ds JOIN aios.dag_node n ON n.node_id=ds.node_id JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        LEFT JOIN LATERAL (
          SELECT updated_at FROM aios.pipeline_foreground_lineage pfl
          WHERE pfl.timeline_id=n.timeline_id
            AND n.event_id BETWEEN pfl.first_event_id AND pfl.head_event_id
            AND pfl.expires_at>now()
          ORDER BY updated_at DESC LIMIT 1
        ) pfl ON true
        WHERE ds.claims_extracted_at IS NULL AND ie.superseded_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='extract_claims' AND pj.status IN ('queued','running') AND pj.payload->>'section_id'=ds.section_id::text)
        ), foreground AS (
          SELECT section_id,0 AS band FROM eligible WHERE foreground_at IS NOT NULL
          ORDER BY foreground_at DESC,event_id DESC
          LIMIT GREATEST(1,ceil($1::numeric*0.75)::integer)
        ), backlog AS (
          SELECT e.section_id,1 AS band FROM eligible e
          WHERE NOT EXISTS (SELECT 1 FROM foreground f WHERE f.section_id=e.section_id)
          ORDER BY e.event_id
          LIMIT $1-(SELECT count(*) FROM foreground)
        )
        SELECT section_id FROM (SELECT * FROM foreground UNION ALL SELECT * FROM backlog) admitted
        ORDER BY band
    """, section_id_payload, 25, 48, True),
    Stage("decompose_claim_frames", "decompose_claim_frames", """
        WITH eligible AS (
          SELECT
              cc.claim_id,
              cc.created_at,
              EXISTS (
                  SELECT 1
                  FROM aios.pipeline_foreground_lineage pfl
                  WHERE pfl.timeline_id=dn.timeline_id
                    AND dn.event_id BETWEEN pfl.first_event_id AND pfl.head_event_id
                    AND pfl.expires_at > now()
              ) AS is_foreground
          FROM aios.claim_candidate cc
          JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
          JOIN aios.document_section ds ON ds.section_id=es.section_id
          JOIN aios.dag_node dn ON dn.node_id=ds.node_id
          JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
          WHERE ie.superseded_at IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM aios.claim_semantic_frame_projection sfp
                WHERE sfp.claim_id=cc.claim_id
                  AND sfp.decomposer_version='semantic-frame-v2'
            )
            AND NOT EXISTS (
                SELECT 1 FROM aios.pipeline_job pj
                WHERE pj.job_type='decompose_claim_frames'
                  AND pj.status IN ('queued','running')
                  AND pj.payload->>'claim_id'=cc.claim_id::text
            )
        ),
        foreground AS (
          SELECT claim_id,created_at,0 AS band_order
          FROM eligible
          WHERE is_foreground
          ORDER BY created_at,claim_id
          LIMIT $1
        ),
        quotas AS (
          SELECT
              floor(GREATEST($1-(SELECT count(*) FROM foreground),0)::numeric*0.75)::integer AS backlog_quota,
              GREATEST($1-(SELECT count(*) FROM foreground),0)
                - floor(GREATEST($1-(SELECT count(*) FROM foreground),0)::numeric*0.75)::integer AS fresh_quota
        ),
        backlog AS (
          SELECT e.claim_id,e.created_at,1 AS band_order
          FROM eligible e
          WHERE NOT e.is_foreground
            AND NOT EXISTS (SELECT 1 FROM foreground f WHERE f.claim_id=e.claim_id)
          ORDER BY e.created_at,e.claim_id
          LIMIT (SELECT backlog_quota FROM quotas)
        ),
        fresh AS (
          SELECT e.claim_id,e.created_at,2 AS band_order
          FROM eligible e
          WHERE NOT e.is_foreground
            AND NOT EXISTS (SELECT 1 FROM foreground f WHERE f.claim_id=e.claim_id)
            AND NOT EXISTS (SELECT 1 FROM backlog b WHERE b.claim_id=e.claim_id)
          ORDER BY e.created_at DESC,e.claim_id
          LIMIT (SELECT fresh_quota FROM quotas)
        )
        SELECT claim_id
        FROM (
          SELECT claim_id,created_at,band_order FROM foreground
          UNION ALL
          SELECT claim_id,created_at,band_order FROM backlog
          UNION ALL
          SELECT claim_id,created_at,band_order FROM fresh
        ) admitted
        ORDER BY band_order,created_at,claim_id
        LIMIT $1
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
          AND NOT EXISTS (SELECT 1 FROM aios.claim_semantic_integrity si WHERE si.claim_id=cc.claim_id AND si.status <> 'valid')
          AND (NOT EXISTS(SELECT 1 FROM aios.observation o WHERE o.claim_id=cc.claim_id) OR EXISTS(SELECT 1 FROM aios.observation o JOIN aios.claim_semantic_frame sf ON sf.claim_id=o.claim_id AND sf.decomposer_version='semantic-frame-v2' LEFT JOIN aios.observation_proposition op ON op.observation_id=o.observation_id AND op.frame_id=sf.frame_id WHERE o.claim_id=cc.claim_id AND op.frame_id IS NULL))
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='normalize_proposition' AND pj.status IN ('queued','running') AND pj.payload->>'claim_id'=cc.claim_id::text)
        ORDER BY cc.created_at LIMIT $1
    """, claim_id_payload, 35, 96, True),
    Stage("materialize_event_occurrences", "materialize_event_occurrences", """
        SELECT DISTINCT o.claim_id
        FROM aios.observation o
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
        WHERE upper(COALESCE(ccr.claim_kind,''))='EVENT'
          AND aios.semantic_claim_topology_admitted(o.claim_id)
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
        SELECT kae.instance_id AS live_instance_id
        FROM aios.knowledge_acquisition_event kae
        LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
        LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
        LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
        LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id
        LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        LEFT JOIN aios.pipeline_foreground_lineage pfl
          ON pfl.instance_id=kae.instance_id AND pfl.expires_at > now()
        WHERE kae.processed_at IS NULL
          AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
          AND NOT EXISTS (
              SELECT 1 FROM aios.pipeline_job pj
              WHERE pj.job_type='project_character_knowledge'
                AND pj.status IN ('queued','running')
                AND pj.payload->>'live_instance_id'=kae.instance_id::text
          )
        GROUP BY kae.instance_id
        ORDER BY (max(pfl.expires_at) IS NOT NULL) DESC, min(kae.created_at)
        LIMIT $1
    """, live_instance_payload, 40, 16, True),
    Stage("derive_character_acquisition_topology", "derive_character_acquisition_topology", """
        WITH eligible AS (
          SELECT kae.acquisition_id,kae.created_at,
                 pfl.updated_at AS foreground_at
          FROM aios.knowledge_acquisition_event kae
          LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
          LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
          LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
          LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id
          LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
          LEFT JOIN aios.pipeline_foreground_lineage pfl
            ON pfl.instance_id=kae.instance_id AND pfl.expires_at>now()
           AND dn.timeline_id=pfl.timeline_id
           AND dn.event_id BETWEEN pfl.first_event_id AND pfl.head_event_id
          WHERE kae.proposition_id IS NOT NULL AND kae.processed_at IS NOT NULL
            AND aios.semantic_proposition_topology_admitted(kae.proposition_id)
            AND (kae.claim_id IS NULL OR aios.semantic_claim_topology_admitted(kae.claim_id))
            AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
            AND NOT EXISTS (SELECT 1 FROM aios.semantic_topology_projection stp WHERE stp.acquisition_id=kae.acquisition_id AND stp.projected_at IS NOT NULL AND stp.resolver_version='semantic-topology-v1')
            AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='derive_character_acquisition_topology' AND pj.status IN ('queued','running') AND pj.payload->>'acquisition_id'=kae.acquisition_id::text)
        ), foreground AS (
          SELECT acquisition_id,0 AS band FROM eligible WHERE foreground_at IS NOT NULL
          ORDER BY foreground_at DESC,created_at DESC,acquisition_id
          LIMIT GREATEST(1,ceil($1::numeric*0.75)::integer)
        ), backlog AS (
          SELECT e.acquisition_id,1 AS band FROM eligible e
          WHERE NOT EXISTS (SELECT 1 FROM foreground f WHERE f.acquisition_id=e.acquisition_id)
          ORDER BY e.created_at,e.acquisition_id
          LIMIT $1-(SELECT count(*) FROM foreground)
        )
        SELECT acquisition_id FROM (
          SELECT * FROM foreground UNION ALL SELECT * FROM backlog
        ) admitted ORDER BY band
    """, acquisition_id_payload, 45, 48, True),
    Stage("derive_world_assertion_topology", "derive_world_assertion_topology", """
        SELECT a.assertion_id FROM aios.world_proposition_assertion a WHERE a.epistemic_status NOT IN ('rejected','superseded')
          AND NOT EXISTS (SELECT 1 FROM aios.semantic_topology_projection stp WHERE stp.assertion_id=a.assertion_id AND stp.projected_at IS NOT NULL AND stp.resolver_version='semantic-topology-v1')
          AND NOT EXISTS (SELECT 1 FROM aios.pipeline_job pj WHERE pj.job_type='derive_world_assertion_topology' AND pj.status IN ('queued','running') AND pj.payload->>'assertion_id'=a.assertion_id::text)
        ORDER BY a.created_at LIMIT $1
    """, assertion_id_payload, 50, 32, True),
    Stage("reconcile_character_beliefs", "reconcile_character_beliefs", """
        SELECT d.instance_id AS live_instance_id
        FROM aios.character_belief_reconciliation_dirty d
        LEFT JOIN aios.pipeline_foreground_lineage pfl
          ON pfl.instance_id=d.instance_id AND pfl.expires_at > now()
        WHERE NOT EXISTS (
            SELECT 1 FROM aios.pipeline_job pj
            WHERE pj.job_type='reconcile_character_beliefs'
              AND pj.status IN ('queued','running')
              AND pj.payload->>'live_instance_id'=d.instance_id::text
        )
        GROUP BY d.instance_id
        ORDER BY (max(pfl.expires_at) IS NOT NULL) DESC, min(d.dirty_at)
        LIMIT $1
    """, live_instance_payload, 55, 16, True),
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
        WHERE ie.superseded_at IS NULL AND aios.semantic_claim_topology_admitted(o.claim_id) AND stp.projected_at IS NULL AND stp.resolver_version='semantic-topology-v1' AND stp.meta->>'reproject_reason'='semantic_proposition_leaves_20260909'
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
        ORDER BY o.observed_at DESC LIMIT $1
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


async def renew_unfinished_foreground(db: Database) -> None:
    """Keep the priority lease alive while an adopted source has unfinished work.

    Do not touch updated_at: that timestamp orders characters by their last
    interactive turn, rather than by a supervisor maintenance pass.
    """
    await db.execute("""
        UPDATE aios.pipeline_foreground_lineage pfl
        SET expires_at=now()+interval '1 hour'
        WHERE pfl.expires_at < now()+interval '15 minutes'
          AND pfl.updated_at > now()-interval '1 day'
          AND EXISTS (
            SELECT 1 FROM aios.dag_node dn
            LEFT JOIN aios.document_section ds ON ds.node_id=dn.node_id
            WHERE dn.timeline_id=pfl.timeline_id
              AND dn.event_id BETWEEN pfl.first_event_id AND pfl.head_event_id
              AND dn.message_text IS NOT NULL
              AND dn.kind IN ('chat_message','observation','paragraph')
              AND (
                ds.section_id IS NULL OR ds.claims_extracted_at IS NULL
                OR EXISTS (
                  SELECT 1 FROM aios.claim_candidate cc
                  JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
                  WHERE es.section_id=ds.section_id
                    AND NOT EXISTS (SELECT 1 FROM aios.observation o WHERE o.claim_id=cc.claim_id)
                )
                OR EXISTS (
                  SELECT 1 FROM aios.knowledge_acquisition_event kae
                  WHERE kae.instance_id=pfl.instance_id AND kae.dag_node_id=dn.node_id
                    AND (kae.processed_at IS NULL OR NOT EXISTS (
                      SELECT 1 FROM aios.semantic_topology_projection stp
                      WHERE stp.acquisition_id=kae.acquisition_id
                        AND stp.projected_at IS NOT NULL
                        AND stp.resolver_version='semantic-topology-v1'
                    ))
                )
              )
          )
    """)


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
    rows: Iterable[Dict[str, object]] = await asyncio.wait_for(
        db.fetch_bounded(stage.eligibility_sql, batch_size, timeout_seconds=5.0),
        timeout=7.0,
    )
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
    last_foreground_refresh = 0.0
    discovery_retry_at: Dict[str, float] = {}
    logger.info("AIOS supervisor started")
    try:
        while True:
            if time.monotonic() - last_foreground_refresh >= 60.0:
                try:
                    await renew_unfinished_foreground(db)
                except Exception:
                    logger.exception("Failed to renew unfinished foreground work")
                last_foreground_refresh = time.monotonic()
            # Heartbeats are cheap events; ready inboxes coalesce into one wake job.
            try:
                from aios_app.agent.autonomy import AutonomyScheduler
                from aios_app.agent.runtime import AgentRuntimeStore
                from aios_app.agent.temporal import TemporalTriggerStore
                temporal = TemporalTriggerStore(db)
                await temporal.reset_stale_firing()
                await AgentRuntimeStore(db).emit_due_heartbeats(limit=100)
                await temporal.emit_due(limit=100)
                await AutonomyScheduler(db).schedule_ready(limit=100)
                # Disposable micro-HUD work opportunistically consumes free
                # donated inference capacity and never survives its receipt.
                from aios_app.agent.transaction_scheduler import TransactionScheduler
                await TransactionScheduler(db).run_pending(limit=8)
            except Exception:
                logger.exception("Agent autonomy scheduling failed")

            # Shadow appraisal is durable background work, never an agent wake
            # or a dependency of autonomy/inference scheduling.
            try:
                from aios_app.agent.reinforcement import ShadowReinforcementService
                from aios_app.agent.outcomes import OutcomeResolver
                await OutcomeResolver(db).process_receipts(limit=16)
                await ShadowReinforcementService(db).process_pending(limit=16)
            except Exception:
                logger.exception("Shadow outcome appraisal failed")

            # Inference endpoints are ephemeral donated/external capacity. Probe them
            # continuously and reap abandoned leases so dead processes cannot consume
            # provider concurrency forever.
            try:
                from aios_app.inference import InferenceBroker, InferenceProviderStore
                inference_store = InferenceProviderStore(db)
                await inference_store.reap_stale_requests()
                inference_broker = InferenceBroker(db)
                due_providers = await inference_store.due_for_health(limit=100)
                if due_providers:
                    await asyncio.gather(
                        *(inference_broker.health_check(p.provider_id) for p in due_providers),
                        return_exceptions=True,
                    )
            except Exception:
                logger.exception("Inference worker health scheduling failed")

            qcnt = await queued_job_count(db)
            queued_by_type = await queued_job_counts_by_type(db)
            critical_reserve = getattr(settings, "supervisor_critical_queue_reserve", 128)
            remaining = max_jobs_per_cycle
            scheduled = 0
            for stage in sorted(STAGES, key=lambda value: value.priority):
                if remaining <= 0: break
                if time.monotonic() < discovery_retry_at.get(stage.name, 0.0):
                    continue
                stage_queued = queued_by_type.get(stage.job_type, 0)
                allowed = stage_admission_capacity(stage=stage,total_queued=qcnt,stage_queued=stage_queued,batch_size=batch_size,remaining_cycle=remaining,soft_cap=max_queued_backlog,critical_reserve=critical_reserve)
                if allowed <= 0: continue
                try:
                    n = await enqueue_stage_jobs(db, stage, batch_size=allowed)
                    scheduled += n; remaining -= n; qcnt += n
                    queued_by_type[stage.job_type] = stage_queued+n
                except (asyncio.TimeoutError, asyncpg.QueryCanceledError,
                        asyncpg.LockNotAvailableError):
                    discovery_retry_at[stage.name] = time.monotonic() + 60.0
                    logger.warning("Stage '%s' discovery exceeded its SQL/lock budget; retry in 60s", stage.name)
                except Exception:
                    logger.exception("Stage '%s' enqueue failed", stage.name)
            if scheduled == 0: await asyncio.sleep(poll_interval)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervisor())
