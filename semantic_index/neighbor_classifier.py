from __future__ import annotations

import json
import logging
from typing import Any

from aios_app.db import Database
from .config import SemanticIndexConfig
from .relation_validator import validate_neighbor_relation

logger = logging.getLogger("aios.semantic_neighbor_classifier")

NEIGHBOR_CLASSIFIER_VERSION = "semantic-neighbor-classifier-v3-scope"


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
    rows = await db.fetch(
        """
        SELECT
            snc.proposition_id,
            snc.neighbor_proposition_id,
            snc.similarity,
            pa.topic_key AS a_topic_key,
            pa.subject_norm AS a_subject_norm,
            pa.predicate_norm AS a_predicate_norm,
            pa.object_norm AS a_object_norm,
            pa.polarity AS a_polarity,
            ca.claim_kind AS a_claim_kind,
            ca.predicate_family AS a_predicate_family,
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
            cb.claim_kind AS b_claim_kind,
            cb.predicate_family AS b_predicate_family,
            cb.world_id AS b_world_id,
            cb.timeline_id AS b_timeline_id,
            cb.epistemic_scope AS b_epistemic_scope,
            cb.character_id AS b_character_id,
            cb.character_instance_id AS b_character_instance_id,
            cb.viewpoint_id AS b_viewpoint_id,
            pc.conflict_type
        FROM aios.semantic_neighbor_candidate snc
        JOIN aios.proposition pa ON pa.proposition_id=snc.proposition_id
        JOIN aios.proposition pb ON pb.proposition_id=snc.neighbor_proposition_id
        LEFT JOIN LATERAL (
            SELECT
                ccr.claim_kind,
                ccr.predicate_family,
                ccr.world_id,
                ccr.timeline_id,
                ccr.epistemic_scope,
                ccr.origin_character_id AS character_id,
                ccr.character_instance_id,
                ccr.viewpoint_id
            FROM aios.observation o
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
            WHERE o.proposition_id=pa.proposition_id
            ORDER BY ccr.resolved_at DESC
            LIMIT 1
        ) ca ON true
        LEFT JOIN LATERAL (
            SELECT
                ccr.claim_kind,
                ccr.predicate_family,
                ccr.world_id,
                ccr.timeline_id,
                ccr.epistemic_scope,
                ccr.origin_character_id AS character_id,
                ccr.character_instance_id,
                ccr.viewpoint_id
            FROM aios.observation o
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
            WHERE o.proposition_id=pb.proposition_id
            ORDER BY ccr.resolved_at DESC
            LIMIT 1
        ) cb ON true
        LEFT JOIN LATERAL (
            SELECT conflict_type
            FROM aios.proposition_conflict pc
            WHERE (
                pc.proposition_a_id=snc.proposition_id
                AND pc.proposition_b_id=snc.neighbor_proposition_id
            )
            OR (
                pc.proposition_a_id=snc.neighbor_proposition_id
                AND pc.proposition_b_id=snc.proposition_id
            )
            ORDER BY pc.strength DESC
            LIMIT 1
        ) pc ON true
        WHERE snc.embedding_version=$1
          AND snc.status='candidate'
          AND NOT EXISTS (
              SELECT 1
              FROM aios.semantic_neighbor_relation r
              WHERE r.proposition_id=snc.proposition_id
                AND r.neighbor_proposition_id=snc.neighbor_proposition_id
                AND r.embedding_version=snc.embedding_version
                AND r.classifier_version=$2
          )
        ORDER BY snc.updated_at
        LIMIT $3
        """,
        cfg.embedding_version,
        NEIGHBOR_CLASSIFIER_VERSION,
        cfg.batch_size,
    )

    written = 0
    for row in rows:
        a = {
            "topic_key": row["a_topic_key"],
            "subject_norm": row["a_subject_norm"],
            "predicate_norm": row["a_predicate_norm"],
            "object_norm": row["a_object_norm"],
            "polarity": row["a_polarity"],
            "claim_kind": row["a_claim_kind"],
            "predicate_family": row["a_predicate_family"],
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
            "subject_norm": row["b_subject_norm"],
            "predicate_norm": row["b_predicate_norm"],
            "object_norm": row["b_object_norm"],
            "polarity": row["b_polarity"],
            "claim_kind": row["b_claim_kind"],
            "predicate_family": row["b_predicate_family"],
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

        await db.execute(
            """
            INSERT INTO aios.semantic_neighbor_relation (
                proposition_id, neighbor_proposition_id,
                embedding_version, relation, confidence,
                classifier_version, status, features, evidence
            )
            VALUES ($1,$2,$3,$4,$5,$6,'candidate',$7::jsonb,$8::jsonb)
            ON CONFLICT DO NOTHING
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
        written += 1

    if written:
        logger.info(
            "Classified %d semantic neighbor relations",
            written,
        )
    return written
