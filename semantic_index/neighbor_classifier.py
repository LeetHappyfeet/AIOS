from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from aios_app.db import Database
from .config import SemanticIndexConfig

logger = logging.getLogger("aios.semantic_neighbor_classifier")

NEIGHBOR_CLASSIFIER_VERSION = "semantic-neighbor-classifier-v1"


def _contains_refinement(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    left = a.strip().lower()
    right = b.strip().lower()
    if left == right:
        return False
    return left in right or right in left


def classify_neighbor_pair(
    *,
    similarity: float,
    a: dict[str, Any],
    b: dict[str, Any],
    conflict_type: str | None,
) -> tuple[str, float, dict[str, Any]]:
    same_subject = bool(a.get("subject_norm") and a.get("subject_norm") == b.get("subject_norm"))
    same_predicate = bool(a.get("predicate_norm") and a.get("predicate_norm") == b.get("predicate_norm"))
    same_object = bool(a.get("object_norm") and a.get("object_norm") == b.get("object_norm"))
    same_polarity = int(a.get("polarity") or 1) == int(b.get("polarity") or 1)
    same_topic = bool(a.get("topic_key") and a.get("topic_key") == b.get("topic_key"))
    same_timeline = bool(a.get("timeline_id") and a.get("timeline_id") == b.get("timeline_id"))
    same_world = bool(a.get("world_id") and a.get("world_id") == b.get("world_id"))
    same_kind = bool(a.get("claim_kind") and a.get("claim_kind") == b.get("claim_kind"))
    same_family = bool(
        a.get("predicate_family")
        and a.get("predicate_family") == b.get("predicate_family")
    )

    features = {
        "similarity": round(float(similarity), 6),
        "same_subject": same_subject,
        "same_predicate": same_predicate,
        "same_object": same_object,
        "same_polarity": same_polarity,
        "same_topic": same_topic,
        "same_timeline": same_timeline,
        "same_world": same_world,
        "same_kind": same_kind,
        "same_family": same_family,
        "conflict_type": conflict_type,
    }

    if conflict_type:
        return "CONTRADICTS", 0.96, features

    if same_subject and same_predicate and same_object and same_polarity:
        return "EQUIVALENT", 0.98, features

    if (
        same_subject
        and same_predicate
        and same_polarity
        and _contains_refinement(a.get("object_norm"), b.get("object_norm"))
    ):
        return "REFINES", min(0.94, 0.72 + 0.22 * similarity), features

    if (
        a.get("claim_kind") == "EVENT"
        and b.get("claim_kind") == "EVENT"
        and same_timeline
        and similarity >= 0.80
    ):
        return "SAME_EVENT", min(0.92, 0.68 + 0.24 * similarity), features

    if same_topic:
        return "SAME_TOPIC", min(0.92, 0.64 + 0.28 * similarity), features

    if similarity >= 0.86 and (same_subject or same_family or same_kind):
        return "RELATED", min(0.90, 0.60 + 0.30 * similarity), features

    if similarity >= 0.76:
        return "RELATED", min(0.82, 0.52 + 0.30 * similarity), features

    return "UNRESOLVED", max(0.0, min(0.65, similarity)), features


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
            pb.topic_key AS b_topic_key,
            pb.subject_norm AS b_subject_norm,
            pb.predicate_norm AS b_predicate_norm,
            pb.object_norm AS b_object_norm,
            pb.polarity AS b_polarity,
            cb.claim_kind AS b_claim_kind,
            cb.predicate_family AS b_predicate_family,
            cb.world_id AS b_world_id,
            cb.timeline_id AS b_timeline_id,
            pc.conflict_type
        FROM aios.semantic_neighbor_candidate snc
        JOIN aios.proposition pa ON pa.proposition_id=snc.proposition_id
        JOIN aios.proposition pb ON pb.proposition_id=snc.neighbor_proposition_id
        LEFT JOIN LATERAL (
            SELECT ccr.claim_kind, ccr.predicate_family, ccr.world_id, ccr.timeline_id
            FROM aios.observation o
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
            WHERE o.proposition_id=pa.proposition_id
            ORDER BY ccr.resolved_at DESC
            LIMIT 1
        ) ca ON true
        LEFT JOIN LATERAL (
            SELECT ccr.claim_kind, ccr.predicate_family, ccr.world_id, ccr.timeline_id
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
