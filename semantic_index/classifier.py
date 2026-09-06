from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from .config import SemanticIndexConfig

logger = logging.getLogger("aios.semantic_classifier")

CLASSIFIER_VERSION = "semantic-structure-classifier-v1"

STATE_PREDICATE_FAMILIES = {
    "spatial",
    "possession",
    "identity",
    "descriptive",
    "emotional",
    "membership",
}
STATE_CLAIM_KINDS = {"STATE", "TRAIT", "RELATIONSHIP"}

BOUNDARY_LABELS = (
    "SAME_REGION",
    "TOPIC_SPLIT",
    "TEMPORAL_TRANSITION",
    "STATE_TRANSITION",
    "NARRATIVE_SPLIT",
    "CONTRADICTION_CLUSTER",
    "EXPERIENTIAL_BRANCH_CANDIDATE",
    "WORLD_BRANCH_CANDIDATE",
)


@dataclass(frozen=True)
class BoundaryFeatures:
    mean_similarity: float
    max_similarity: float
    edge_count: int
    topic_overlap: float
    subject_overlap: float
    predicate_overlap: float
    source_overlap: float
    world_overlap: float
    timeline_overlap: float
    character_instance_overlap: float
    origin_character_overlap: float
    temporal_overlap: float
    temporal_separation: float
    conflict_density: float
    exclusive_conflict_density: float
    opposite_polarity_density: float
    state_like_ratio: float

    def as_dict(self) -> dict[str, Any]:
        return {
            key: round(value, 6) if isinstance(value, float) else value
            for key, value in self.__dict__.items()
        }


def _as_counts(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, count in value.items():
        try:
            count_f = float(count)
        except (TypeError, ValueError):
            continue
        if count_f > 0:
            out[str(key)] = count_f
    return out


def _weighted_overlap(a: Any, b: Any) -> float:
    left = _as_counts(a)
    right = _as_counts(b)
    keys = set(left) | set(right)
    if not keys:
        return 0.0
    intersection = sum(min(left.get(k, 0.0), right.get(k, 0.0)) for k in keys)
    union = sum(max(left.get(k, 0.0), right.get(k, 0.0)) for k in keys)
    return intersection / union if union else 0.0


def _dominance(counts: Any, keys: set[str] | None = None) -> float:
    values = _as_counts(counts)
    total = sum(values.values())
    if not total:
        return 0.0
    if keys is None:
        return max(values.values()) / total
    return sum(v for k, v in values.items() if k.upper() in keys) / total


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _time_relation(meta_a: Mapping[str, Any], meta_b: Mapping[str, Any]) -> tuple[float, float]:
    a0 = _parse_dt(meta_a.get("time_start"))
    a1 = _parse_dt(meta_a.get("time_end"))
    b0 = _parse_dt(meta_b.get("time_start"))
    b1 = _parse_dt(meta_b.get("time_end"))
    if None in {a0, a1, b0, b1}:
        return 0.0, 0.0

    assert a0 is not None and a1 is not None and b0 is not None and b1 is not None
    latest_start = max(a0, b0)
    earliest_end = min(a1, b1)
    combined_start = min(a0, b0)
    combined_end = max(a1, b1)
    total = max((combined_end - combined_start).total_seconds(), 1.0)

    if latest_start <= earliest_end:
        overlap = (earliest_end - latest_start).total_seconds() / total
        return min(1.0, max(0.0, overlap)), 0.0

    gap = (latest_start - earliest_end).total_seconds()
    return 0.0, min(1.0, gap / total)


def _score_boundary(features: BoundaryFeatures) -> dict[str, float]:
    semantic_weakness = 1.0 - features.mean_similarity
    time_overlap = features.temporal_overlap
    time_separation = features.temporal_separation
    same_subject = features.subject_overlap
    same_predicate = features.predicate_overlap
    same_topic = features.topic_overlap
    same_world = features.world_overlap
    same_timeline = features.timeline_overlap
    same_instance = features.character_instance_overlap
    same_source = features.source_overlap
    conflict = features.conflict_density
    state_conflict = features.exclusive_conflict_density * max(
        features.state_like_ratio, 0.4
    )

    scores = {
        "SAME_REGION": (
            0.34 * features.mean_similarity
            + 0.18 * same_topic
            + 0.18 * same_subject
            + 0.12 * same_predicate
            + 0.10 * same_world
            + 0.08 * same_timeline
            - 0.20 * conflict
        ),
        "TOPIC_SPLIT": (
            0.35 * semantic_weakness
            + 0.30 * (1.0 - same_topic)
            + 0.20 * (1.0 - same_subject)
            + 0.15 * (1.0 - same_predicate)
        ),
        "TEMPORAL_TRANSITION": (
            0.30 * time_separation
            + 0.24 * same_subject
            + 0.20 * same_predicate
            + 0.14 * same_timeline
            + 0.12 * same_world
        ),
        "STATE_TRANSITION": (
            0.24 * time_separation
            + 0.20 * same_subject
            + 0.18 * same_predicate
            + 0.16 * same_timeline
            + 0.10 * same_world
            + 0.12 * state_conflict
        ),
        "NARRATIVE_SPLIT": (
            0.24 * same_topic
            + 0.18 * same_subject
            + 0.20 * (1.0 - same_source)
            + 0.16 * conflict
            + 0.12 * time_overlap
            + 0.10 * semantic_weakness
        ),
        "CONTRADICTION_CLUSTER": (
            0.30 * conflict
            + 0.18 * features.opposite_polarity_density
            + 0.14 * features.exclusive_conflict_density
            + 0.14 * time_overlap
            + 0.12 * same_world
            + 0.12 * same_timeline
        ),
        "EXPERIENTIAL_BRANCH_CANDIDATE": (
            0.22 * same_topic
            + 0.18 * same_subject
            + 0.18 * same_world
            + 0.16 * (1.0 - same_instance)
            + 0.12 * features.origin_character_overlap
            + 0.08 * conflict
            + 0.06 * time_overlap
        ),
        "WORLD_BRANCH_CANDIDATE": (
            0.24 * same_topic
            + 0.18 * same_subject
            + 0.18 * (1.0 - same_world)
            + 0.16 * conflict
            + 0.12 * time_overlap
            + 0.12 * semantic_weakness
        ),
    }

    return {
        key: max(0.0, min(1.0, value))
        for key, value in scores.items()
    }


def _choose_label(
    scores: Mapping[str, float],
    *,
    min_confidence: float,
    min_margin: float,
) -> tuple[str, float]:
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return "UNRESOLVED", 0.0
    label, score = ordered[0]
    runner_up = ordered[1][1] if len(ordered) > 1 else 0.0
    if score < min_confidence or score - runner_up < min_margin:
        return "UNRESOLVED", score
    return label, score


async def _cross_cluster_conflicts(
    db: Database,
    cluster_a_id: UUID,
    cluster_b_id: UUID,
) -> dict[str, float]:
    row = await db.fetchrow(
        """
        WITH a AS (
            SELECT proposition_id
            FROM aios.semantic_cluster_membership
            WHERE cluster_id=$1
        ),
        b AS (
            SELECT proposition_id
            FROM aios.semantic_cluster_membership
            WHERE cluster_id=$2
        ),
        cross_pairs AS (
            SELECT COUNT(*)::double precision AS pair_count
            FROM a CROSS JOIN b
        ),
        conflicts AS (
            SELECT
                COUNT(*)::double precision AS conflict_count,
                COUNT(*) FILTER (
                    WHERE pc.conflict_type='exclusive_object'
                )::double precision AS exclusive_count,
                COUNT(*) FILTER (
                    WHERE pc.conflict_type='opposite_polarity'
                )::double precision AS polarity_count
            FROM aios.proposition_conflict pc
            WHERE (
                pc.proposition_a_id IN (SELECT proposition_id FROM a)
                AND pc.proposition_b_id IN (SELECT proposition_id FROM b)
            )
            OR (
                pc.proposition_a_id IN (SELECT proposition_id FROM b)
                AND pc.proposition_b_id IN (SELECT proposition_id FROM a)
            )
        )
        SELECT
            cp.pair_count,
            c.conflict_count,
            c.exclusive_count,
            c.polarity_count
        FROM cross_pairs cp CROSS JOIN conflicts c
        """,
        cluster_a_id,
        cluster_b_id,
    )
    if not row:
        return {
            "pair_count": 0.0,
            "conflict_count": 0.0,
            "exclusive_count": 0.0,
            "polarity_count": 0.0,
        }
    return {key: float(row[key] or 0.0) for key in row.keys()}


def _cluster_region_classification(meta: Mapping[str, Any]) -> tuple[str, float, dict[str, float]]:
    kinds = _as_counts(meta.get("claim_kinds"))
    families = _as_counts(meta.get("predicate_families"))
    total = sum(kinds.values()) or 1.0

    kind_scores = {
        "EVENT_REGION": (kinds.get("EVENT", 0.0) / total),
        "MEMORY_REGION": (kinds.get("MEMORY", 0.0) / total),
        "BELIEF_REGION": (
            sum(kinds.get(key, 0.0) for key in ("BELIEF", "CONCEPT", "TRAIT"))
            / total
        ),
        "RULE_REGION": (kinds.get("RULE", 0.0) / total),
        "GOAL_REGION": (kinds.get("GOAL", 0.0) / total),
    }
    state_ratio = (
        sum(
            count
            for family, count in families.items()
            if family.lower() in STATE_PREDICATE_FAMILIES
        )
        / max(sum(families.values()), 1.0)
    )
    kind_scores["STATE_SERIES"] = max(
        state_ratio,
        sum(kinds.get(key, 0.0) for key in STATE_CLAIM_KINDS) / total,
    )

    topic_dominance = _dominance(meta.get("dominant_topics"))
    kind_scores["TOPIC_REGION"] = topic_dominance

    ordered = sorted(kind_scores.items(), key=lambda item: item[1], reverse=True)
    label, confidence = ordered[0]
    if confidence < 0.55:
        label = "MIXED_REGION" if kinds or families else "UNRESOLVED"
        confidence = max(confidence, 0.5 if kinds or families else 0.0)
    return label, min(1.0, confidence), {
        key: round(value, 6) for key, value in kind_scores.items()
    }


async def classify_latest_clusters_once(
    db: Database,
    cfg: SemanticIndexConfig,
) -> int:
    run = await db.fetchrow(
        """
        SELECT run_id
        FROM aios.semantic_cluster_run
        WHERE embedding_version=$1
          AND status='done'
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        cfg.embedding_version,
    )
    if not run:
        return 0
    run_id = run["run_id"]

    cluster_rows = await db.fetch(
        """
        SELECT cluster_id, meta
        FROM aios.semantic_cluster_candidate c
        WHERE c.run_id=$1
          AND c.status='candidate'
          AND NOT EXISTS (
              SELECT 1
              FROM aios.semantic_cluster_classification cc
              WHERE cc.run_id=c.run_id
                AND cc.cluster_id=c.cluster_id
                AND cc.classifier_version=$2
          )
        """,
        run_id,
        CLASSIFIER_VERSION,
    )

    written = 0
    for row in cluster_rows:
        meta = dict(row["meta"] or {})
        label, confidence, feature_scores = _cluster_region_classification(meta)
        await db.execute(
            """
            INSERT INTO aios.semantic_cluster_classification (
                run_id, cluster_id, classification, confidence,
                classifier_version, status, feature_scores, evidence
            )
            VALUES ($1,$2,$3,$4,$5,'candidate',$6::jsonb,$7::jsonb)
            ON CONFLICT DO NOTHING
            """,
            run_id,
            row["cluster_id"],
            label,
            confidence,
            CLASSIFIER_VERSION,
            json.dumps(feature_scores),
            json.dumps({"cluster_meta": meta}),
        )
        written += 1

    boundaries = await db.fetch(
        """
        SELECT
            b.cluster_a_id,
            b.cluster_b_id,
            b.edge_count,
            b.mean_similarity,
            b.max_similarity,
            a.meta AS meta_a,
            c.meta AS meta_b
        FROM aios.semantic_cluster_boundary b
        JOIN aios.semantic_cluster_candidate a
          ON a.cluster_id=b.cluster_a_id
        JOIN aios.semantic_cluster_candidate c
          ON c.cluster_id=b.cluster_b_id
        WHERE b.run_id=$1
          AND NOT EXISTS (
              SELECT 1
              FROM aios.semantic_boundary_classification bc
              WHERE bc.run_id=b.run_id
                AND bc.cluster_a_id=b.cluster_a_id
                AND bc.cluster_b_id=b.cluster_b_id
                AND bc.classifier_version=$2
          )
        """,
        run_id,
        CLASSIFIER_VERSION,
    )

    for row in boundaries:
        meta_a = dict(row["meta_a"] or {})
        meta_b = dict(row["meta_b"] or {})
        temporal_overlap, temporal_separation = _time_relation(meta_a, meta_b)
        conflicts = await _cross_cluster_conflicts(
            db,
            row["cluster_a_id"],
            row["cluster_b_id"],
        )
        pair_count = max(conflicts["pair_count"], 1.0)

        family_a = _as_counts(meta_a.get("predicate_families"))
        family_b = _as_counts(meta_b.get("predicate_families"))
        state_values = {
            key.lower()
            for key in set(family_a) | set(family_b)
            if key.lower() in STATE_PREDICATE_FAMILIES
        }
        state_numerator = sum(family_a.get(k, 0.0) for k in family_a if k.lower() in state_values)
        state_numerator += sum(family_b.get(k, 0.0) for k in family_b if k.lower() in state_values)
        state_denominator = sum(family_a.values()) + sum(family_b.values())
        state_like_ratio = (
            state_numerator / state_denominator if state_denominator else 0.0
        )

        features = BoundaryFeatures(
            mean_similarity=float(row["mean_similarity"]),
            max_similarity=float(row["max_similarity"]),
            edge_count=int(row["edge_count"]),
            topic_overlap=_weighted_overlap(
                meta_a.get("dominant_topics"), meta_b.get("dominant_topics")
            ),
            subject_overlap=_weighted_overlap(
                meta_a.get("dominant_subjects"), meta_b.get("dominant_subjects")
            ),
            predicate_overlap=_weighted_overlap(
                meta_a.get("dominant_predicates"), meta_b.get("dominant_predicates")
            ),
            source_overlap=_weighted_overlap(
                meta_a.get("source_domains"), meta_b.get("source_domains")
            ),
            world_overlap=_weighted_overlap(
                meta_a.get("world_distribution"), meta_b.get("world_distribution")
            ),
            timeline_overlap=_weighted_overlap(
                meta_a.get("timeline_distribution"), meta_b.get("timeline_distribution")
            ),
            character_instance_overlap=_weighted_overlap(
                meta_a.get("character_instances"), meta_b.get("character_instances")
            ),
            origin_character_overlap=_weighted_overlap(
                meta_a.get("origin_characters"), meta_b.get("origin_characters")
            ),
            temporal_overlap=temporal_overlap,
            temporal_separation=temporal_separation,
            conflict_density=min(1.0, conflicts["conflict_count"] / pair_count),
            exclusive_conflict_density=min(1.0, conflicts["exclusive_count"] / pair_count),
            opposite_polarity_density=min(1.0, conflicts["polarity_count"] / pair_count),
            state_like_ratio=state_like_ratio,
        )
        scores = _score_boundary(features)
        label, confidence = _choose_label(
            scores,
            min_confidence=cfg.classifier_min_confidence,
            min_margin=cfg.classifier_min_margin,
        )

        evidence = {
            "features": features.as_dict(),
            "cross_cluster_conflicts": conflicts,
            "cluster_a_meta": meta_a,
            "cluster_b_meta": meta_b,
        }
        await db.execute(
            """
            INSERT INTO aios.semantic_boundary_classification (
                run_id, cluster_a_id, cluster_b_id,
                classification, confidence, classifier_version,
                status, feature_scores, evidence
            )
            VALUES ($1,$2,$3,$4,$5,$6,'candidate',$7::jsonb,$8::jsonb)
            ON CONFLICT DO NOTHING
            """,
            run_id,
            row["cluster_a_id"],
            row["cluster_b_id"],
            label,
            confidence,
            CLASSIFIER_VERSION,
            json.dumps(scores),
            json.dumps(evidence),
        )
        written += 1

    if written:
        logger.info(
            "Semantic classifier produced %d advisory cluster/boundary classifications",
            written,
        )
    return written
