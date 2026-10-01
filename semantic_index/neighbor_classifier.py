from __future__ import annotations

import json
import logging
import time
from typing import Any

from aios_app.db import Database
from .config import SemanticIndexConfig
from .relation_validator import validate_neighbor_relation

logger = logging.getLogger("aios.semantic_neighbor_classifier")

NEIGHBOR_CLASSIFIER_VERSION = "semantic-neighbor-classifier-v6"


def classify_neighbor_pair(
    *,
    similarity: float,
    a: dict[str, Any],
    b: dict[str, Any],
    conflict_type: str | None,
) -> tuple[str, float, dict[str, Any]]:
    """Classify one candidate pair through the independent relation verifier.

    Candidate generation may use vector similarity, topic geometry and legacy
    conflict records.  None of those candidate hints are allowed to prove the
    final semantic relation by themselves.
    """
    return validate_neighbor_relation(
        similarity=similarity,
        a=a,
        b=b,
        conflict_type=conflict_type,
    )


async def classify_neighbor_relations_once(
    db: Database,
    cfg: SemanticIndexConfig,
) -> int:
    started = time.monotonic()
    logger.debug("Semantic relation candidate query started")
    rows = await db.fetch(
        """
        WITH pending AS MATERIALIZED (
            SELECT snc.* FROM aios.semantic_neighbor_candidate snc
            WHERE snc.embedding_version=$1 AND snc.status='candidate'
              AND snc.classification_ready_at<=now()
              AND snc.last_classifier_version IS DISTINCT FROM $2
            ORDER BY snc.updated_at DESC,snc.proposition_id,snc.neighbor_proposition_id
            LIMIT $3
        ), supported AS MATERIALIZED (
            SELECT snc.*, (snc.similarity >= $4 AND (
                (NULLIF(pa.subject_norm,'') IS NOT NULL AND pa.subject_norm=pb.subject_norm)
                OR (NULLIF(pa.object_norm,'') IS NOT NULL AND pa.object_norm=pb.object_norm)
                OR (NULLIF(pa.topic_key,'') IS NOT NULL AND pa.topic_key=pb.topic_key)
                OR (NULLIF(pa.subject_norm,'') IS NOT NULL AND NULLIF(pa.object_norm,'') IS NOT NULL
                    AND pa.subject_norm=pb.object_norm AND pa.object_norm=pb.subject_norm)
            )) AS structural_supported
            FROM pending snc
            JOIN aios.proposition pa ON pa.proposition_id=snc.proposition_id
            JOIN aios.proposition pb ON pb.proposition_id=snc.neighbor_proposition_id
        )
        SELECT
            snc.structural_supported,
            snc.proposition_id,
            snc.neighbor_proposition_id,
            snc.similarity,
            pa.topic_key AS a_topic_key,
            pa.subject_norm AS a_subject_norm,
            pa.predicate_norm AS a_predicate_norm,
            pa.object_norm AS a_object_norm,
            pa.polarity AS a_polarity,
            ca.observation_id AS a_observation_id,
            ca.claim_id AS a_claim_id,
            ca.dag_node_id AS a_dag_node_id,
            ca.claim_kind AS a_claim_kind,
            ca.predicate_family AS a_predicate_family,
            ca.semantic_confidence AS a_semantic_confidence,
            ca.world_id AS a_world_id,
            ca.timeline_id AS a_timeline_id,
            ca.epistemic_scope AS a_epistemic_scope,
            ca.character_id AS a_character_id,
            ca.character_instance_id AS a_character_instance_id,
            ca.viewpoint_id AS a_viewpoint_id,
            pb.topic_key AS b_topic_key,
            pb.subject_norm AS b_subject_norm,
            pb.predicate_norm AS b_predicate_norm,
            pb.object_norm AS b_object_norm,
            pb.polarity AS b_polarity,
            cb.observation_id AS b_observation_id,
            cb.claim_id AS b_claim_id,
            cb.dag_node_id AS b_dag_node_id,
            cb.claim_kind AS b_claim_kind,
            cb.predicate_family AS b_predicate_family,
            cb.semantic_confidence AS b_semantic_confidence,
            cb.world_id AS b_world_id,
            cb.timeline_id AS b_timeline_id,
            cb.epistemic_scope AS b_epistemic_scope,
            cb.character_id AS b_character_id,
            cb.character_instance_id AS b_character_instance_id,
            cb.viewpoint_id AS b_viewpoint_id,
            pc.conflict_type
        FROM supported snc
        JOIN aios.proposition pa ON pa.proposition_id=snc.proposition_id
        JOIN aios.proposition pb ON pb.proposition_id=snc.neighbor_proposition_id
        LEFT JOIN LATERAL (
            SELECT
                o.observation_id,
                o.claim_id,
                o.dag_node_id,
                ccr.claim_kind,
                ccr.predicate_family,
                CASE
                    WHEN (ccr.meta->>'semantic_confidence') ~ '^[0-9]+([.][0-9]+)?$'
                    THEN (ccr.meta->>'semantic_confidence')::double precision
                    ELSE NULL
                END AS semantic_confidence,
                ccr.world_id,
                ccr.timeline_id,
                ccr.epistemic_scope,
                ccr.origin_character_id AS character_id,
                ccr.character_instance_id,
                ccr.viewpoint_id
            FROM aios.observation_proposition op
            JOIN aios.observation o ON o.observation_id=op.observation_id
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
            WHERE op.proposition_id=pa.proposition_id AND snc.structural_supported
              AND aios.semantic_occurrence_topology_eligible(o.claim_id,pa.proposition_id)
              AND aios.semantic_claim_topology_admitted(o.claim_id)
            ORDER BY o.observed_at DESC,o.observation_id
            LIMIT 1
        ) ca ON true
        LEFT JOIN LATERAL (
            SELECT
                o.observation_id,
                o.claim_id,
                o.dag_node_id,
                ccr.claim_kind,
                ccr.predicate_family,
                CASE
                    WHEN (ccr.meta->>'semantic_confidence') ~ '^[0-9]+([.][0-9]+)?$'
                    THEN (ccr.meta->>'semantic_confidence')::double precision
                    ELSE NULL
                END AS semantic_confidence,
                ccr.world_id,
                ccr.timeline_id,
                ccr.epistemic_scope,
                ccr.origin_character_id AS character_id,
                ccr.character_instance_id,
                ccr.viewpoint_id
            FROM aios.observation_proposition op
            JOIN aios.observation o ON o.observation_id=op.observation_id
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
            WHERE op.proposition_id=pb.proposition_id AND snc.structural_supported
              AND aios.semantic_occurrence_topology_eligible(o.claim_id,pb.proposition_id)
              AND aios.semantic_claim_topology_admitted(o.claim_id)
            ORDER BY o.observed_at DESC,o.observation_id
            LIMIT 1
        ) cb ON true
        LEFT JOIN LATERAL (
            SELECT conflict_type
            FROM aios.proposition_conflict pc
            WHERE snc.structural_supported AND ((
                pc.proposition_a_id=snc.proposition_id
                AND pc.proposition_b_id=snc.neighbor_proposition_id
            )
            OR (
                pc.proposition_a_id=snc.neighbor_proposition_id
                AND pc.proposition_b_id=snc.proposition_id
            )
            )
            ORDER BY pc.strength DESC
            LIMIT 1
        ) pc ON true
        """,
        cfg.embedding_version,
        NEIGHBOR_CLASSIFIER_VERSION,
        getattr(cfg, "relation_batch_size", cfg.batch_size),
        getattr(cfg, "neighbor_min_score", 0.72),
    )

    query_seconds = time.monotonic() - started
    logger.log(logging.INFO if rows or query_seconds >= 1.0 else logging.DEBUG,
                "Semantic relation candidate query returned %d pairs in %.2fs",
                len(rows), query_seconds)
    classify_seconds = write_seconds = 0.0
    written = 0
    culled = deferred = 0
    for row in rows:
        row_started = time.monotonic()
        pair = (row["proposition_id"], row["neighbor_proposition_id"], cfg.embedding_version)
        if not row["structural_supported"]:
            await db.execute("""UPDATE aios.semantic_neighbor_candidate
                SET status='culled',relation_hint='no_structural_anchor'
                WHERE proposition_id=$1 AND neighbor_proposition_id=$2 AND embedding_version=$3""", *pair)
            culled += 1
            continue
        if not row["a_claim_id"] or not row["b_claim_id"]:
            # Missing admitted context is not a negative relation. Keep it
            # retryable without letting the newest unresolved pair pin a batch.
            await db.execute("""UPDATE aios.semantic_neighbor_candidate
                SET classification_ready_at=now()+interval '30 seconds'
                WHERE proposition_id=$1 AND neighbor_proposition_id=$2 AND embedding_version=$3""", *pair)
            deferred += 1
            continue
        a = {
            "topic_key": row["a_topic_key"],
            "observation_id": str(row["a_observation_id"]) if row["a_observation_id"] else None,
            "claim_id": str(row["a_claim_id"]) if row["a_claim_id"] else None,
            "dag_node_id": str(row["a_dag_node_id"]) if row["a_dag_node_id"] else None,
            "subject_norm": row["a_subject_norm"],
            "predicate_norm": row["a_predicate_norm"],
            "object_norm": row["a_object_norm"],
            "polarity": row["a_polarity"],
            "claim_kind": row["a_claim_kind"],
            "predicate_family": row["a_predicate_family"],
            "semantic_confidence": row["a_semantic_confidence"],
            "world_id": str(row["a_world_id"]) if row["a_world_id"] else None,
            "timeline_id": str(row["a_timeline_id"]) if row["a_timeline_id"] else None,
            "epistemic_scope": row["a_epistemic_scope"],
            "character_id": row["a_character_id"],
            "character_instance_id": (
                str(row["a_character_instance_id"])
                if row["a_character_instance_id"]
                else None
            ),
            "viewpoint_id": row["a_viewpoint_id"],
        }
        b = {
            "topic_key": row["b_topic_key"],
            "observation_id": str(row["b_observation_id"]) if row["b_observation_id"] else None,
            "claim_id": str(row["b_claim_id"]) if row["b_claim_id"] else None,
            "dag_node_id": str(row["b_dag_node_id"]) if row["b_dag_node_id"] else None,
            "subject_norm": row["b_subject_norm"],
            "predicate_norm": row["b_predicate_norm"],
            "object_norm": row["b_object_norm"],
            "polarity": row["b_polarity"],
            "claim_kind": row["b_claim_kind"],
            "predicate_family": row["b_predicate_family"],
            "semantic_confidence": row["b_semantic_confidence"],
            "world_id": str(row["b_world_id"]) if row["b_world_id"] else None,
            "timeline_id": str(row["b_timeline_id"]) if row["b_timeline_id"] else None,
            "epistemic_scope": row["b_epistemic_scope"],
            "character_id": row["b_character_id"],
            "character_instance_id": (
                str(row["b_character_instance_id"])
                if row["b_character_instance_id"]
                else None
            ),
            "viewpoint_id": row["b_viewpoint_id"],
        }

        relation, confidence, features = classify_neighbor_pair(
            similarity=float(row["similarity"]),
            a=a,
            b=b,
            conflict_type=row["conflict_type"],
        )

        classify_seconds += time.monotonic() - row_started
        write_started = time.monotonic()
        inserted = await db.fetchrow(
            """
            INSERT INTO aios.semantic_neighbor_relation (
                proposition_id, neighbor_proposition_id,
                embedding_version, relation, confidence,
                classifier_version, status, features, evidence
            )
            VALUES ($1,$2,$3,$4,$5,$6,'candidate',$7::jsonb,$8::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING 1 AS inserted
            """,
            row["proposition_id"],
            row["neighbor_proposition_id"],
            cfg.embedding_version,
            relation,
            confidence,
            NEIGHBOR_CLASSIFIER_VERSION,
            json.dumps(features),
            json.dumps({
                "proposition_a": a,
                "proposition_b": b,
            }),
        )
        write_seconds += time.monotonic() - write_started
        # This small receipt prevents scanning already classified pairs with
        # an anti-join for every pass. A new classifier version remains eligible.
        await db.execute("""UPDATE aios.semantic_neighbor_candidate
            SET last_classifier_version=$4
            WHERE proposition_id=$1 AND neighbor_proposition_id=$2 AND embedding_version=$3""",
            *pair,NEIGHBOR_CLASSIFIER_VERSION)
        if inserted:
            written += 1

    logger.log(logging.INFO if written or culled or deferred or time.monotonic() - started >= 1.0 else logging.DEBUG,
        "Classified %d semantic neighbor relations (culled=%d deferred=%d): query=%.2fs classify=%.2fs writes=%.2fs total=%.2fs",
        written, culled, deferred, query_seconds, classify_seconds, write_seconds,
        time.monotonic() - started,
    )
    return written
