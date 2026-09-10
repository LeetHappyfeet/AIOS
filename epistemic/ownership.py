from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

from aios_app.db import Database

OWNERSHIP_RESOLVER_VERSION = "semantic-ownership-hybrid-v1"

# Vector neighbors are advisory. They may help resolve an ambiguous owner, but
# they may never override an explicit character/world/source semantic owner.
NEIGHBOR_MIN_SIMILARITY = 0.76
NEIGHBOR_MIN_COUNT = 2
NEIGHBOR_MIN_PURITY = 0.72
NEIGHBOR_MIN_MARGIN = 0.15
NEIGHBOR_LIMIT = 12


@dataclass(frozen=True)
class SemanticOwnershipResolution:
    owner_kind: str
    owner_key: str
    status: str
    confidence: float
    resolution_source: str
    character_id: Optional[str] = None
    character_instance_id: Optional[UUID] = None
    world_id: Optional[UUID] = None
    source_id: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_meta(self) -> dict[str, Any]:
        return {
            "resolver_version": OWNERSHIP_RESOLVER_VERSION,
            "owner_kind": self.owner_kind,
            "owner_key": self.owner_key,
            "status": self.status,
            "confidence": round(float(self.confidence), 6),
            "resolution_source": self.resolution_source,
            "evidence": self.evidence,
        }


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _unresolved_key(row: dict[str, Any]) -> str:
    anchor = row.get("timeline_id") or row.get("claim_id") or row.get("proposition_id") or "unknown"
    return f"unresolved:{anchor}"


def deterministic_semantic_ownership(row: dict[str, Any]) -> SemanticOwnershipResolution:
    """Resolve only owners justified by explicit semantic/context facts.

    Source provenance is intentionally not a generic ownership fallback. A
    source owns a proposition only when the context resolver explicitly says
    the proposition is source-scoped. Otherwise an unresolved proposition stays
    unresolved until stronger identity, graph, or vector evidence exists.
    """
    scope = _norm(row.get("epistemic_scope"))
    source_id = str(row["source_id"]) if row.get("source_id") else None
    world_id = row.get("world_id")
    character_id = str(row["origin_character_id"]) if row.get("origin_character_id") else None
    character_instance_id = row.get("character_instance_id")

    if scope == "character" and character_id:
        return SemanticOwnershipResolution(
            owner_kind="character",
            owner_key=f"char:{character_id}",
            status="resolved",
            confidence=1.0,
            resolution_source="explicit_character_scope",
            character_id=character_id,
            character_instance_id=character_instance_id,
            world_id=world_id,
            source_id=source_id,
            evidence={"epistemic_scope": scope, "origin_character_id": character_id},
        )

    if scope == "narrative" and world_id:
        return SemanticOwnershipResolution(
            owner_kind="world",
            owner_key=f"world:{world_id}:observed",
            status="resolved",
            confidence=1.0,
            resolution_source="explicit_narrative_world_scope",
            world_id=world_id,
            source_id=source_id,
            evidence={"epistemic_scope": scope, "world_id": str(world_id)},
        )

    if scope == "source" and source_id:
        return SemanticOwnershipResolution(
            owner_kind="source",
            owner_key=f"source:{source_id}",
            status="resolved",
            confidence=1.0,
            resolution_source="explicit_source_scope",
            source_id=source_id,
            evidence={"epistemic_scope": scope, "source_id": source_id},
        )

    if scope in {"world", "observation"} and world_id:
        return SemanticOwnershipResolution(
            owner_kind="world",
            owner_key=f"world:{world_id}:observed",
            status="resolved",
            confidence=0.98,
            resolution_source="explicit_world_scope",
            world_id=world_id,
            source_id=source_id,
            evidence={"epistemic_scope": scope, "world_id": str(world_id)},
        )

    return SemanticOwnershipResolution(
        owner_kind="unresolved",
        owner_key=_unresolved_key(row),
        status="unresolved",
        confidence=0.0,
        resolution_source="insufficient_semantic_evidence",
        world_id=world_id,
        source_id=source_id,
        evidence={
            "epistemic_scope": scope or None,
            "origin_character_id": character_id,
            "speaker_id": row.get("speaker_id"),
            "speaker_type": row.get("speaker_type"),
            "world_id": str(world_id) if world_id else None,
            "source_id": source_id,
        },
    )


async def resolve_character_mention(db: Database, mention: str) -> Optional[dict[str, str]]:
    """Resolve exact AIOS identity/alias matches before trusting NER labels."""
    value = str(mention or "").strip()
    if not value:
        return None

    row = await db.fetchrow(
        """
        SELECT DISTINCT
            ci.character_id,
            COALESCE(NULLIF(ci.display_name,''), NULLIF(ci.canonical_name,''), ci.character_id) AS label,
            CASE
                WHEN lower(trim(ci.character_id))=lower(trim($1)) THEN 'character_id_exact'
                WHEN lower(trim(COALESCE(ci.display_name,'')))=lower(trim($1)) THEN 'display_name_exact'
                WHEN lower(trim(COALESCE(ci.canonical_name,'')))=lower(trim($1)) THEN 'canonical_name_exact'
                ELSE 'alias_exact'
            END AS method,
            CASE
                WHEN lower(trim(ci.character_id))=lower(trim($1)) THEN 0
                WHEN lower(trim(COALESCE(ci.display_name,'')))=lower(trim($1)) THEN 1
                WHEN lower(trim(COALESCE(ci.canonical_name,'')))=lower(trim($1)) THEN 2
                ELSE 3
            END AS priority
        FROM aios.character_identity ci
        LEFT JOIN aios.character_alias ca ON ca.character_id=ci.character_id
        WHERE lower(trim(ci.character_id))=lower(trim($1))
           OR lower(trim(COALESCE(ci.display_name,'')))=lower(trim($1))
           OR lower(trim(COALESCE(ci.canonical_name,'')))=lower(trim($1))
           OR lower(trim(COALESCE(ca.alias,'')))=lower(trim($1))
        ORDER BY priority, ci.character_id
        LIMIT 1
        """,
        value,
    )
    if not row:
        return None
    return {
        "character_id": str(row["character_id"]),
        "label": str(row["label"] or row["character_id"]),
        "method": str(row["method"]),
    }


async def _resolve_speaker_character(
    db: Database,
    row: dict[str, Any],
) -> Optional[SemanticOwnershipResolution]:
    if _norm(row.get("epistemic_scope")) != "speaker" or not row.get("speaker_id"):
        return None

    identity = await resolve_character_mention(db, str(row["speaker_id"]))
    if not identity:
        return None

    source_id = str(row["source_id"]) if row.get("source_id") else None
    world_id = row.get("world_id")
    return SemanticOwnershipResolution(
        owner_kind="character",
        owner_key=f"char:{identity['character_id']}",
        status="resolved",
        confidence=0.99,
        resolution_source="canonical_speaker_identity",
        character_id=identity["character_id"],
        character_instance_id=row.get("character_instance_id"),
        world_id=world_id,
        source_id=source_id,
        evidence={
            "speaker_id": str(row["speaker_id"]),
            "identity_method": identity["method"],
        },
    )


async def _neighbor_rows(db: Database, proposition_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        WITH raw_neighbor AS (
            SELECT neighbor_proposition_id AS proposition_id, similarity
            FROM aios.semantic_neighbor_candidate
            WHERE proposition_id=$1
              AND similarity >= $2
            UNION ALL
            SELECT proposition_id, similarity
            FROM aios.semantic_neighbor_candidate
            WHERE neighbor_proposition_id=$1
              AND similarity >= $2
        ),
        neighbor AS (
            SELECT proposition_id, MAX(similarity) AS similarity
            FROM raw_neighbor
            GROUP BY proposition_id
        )
        SELECT
            n.similarity,
            ccr.epistemic_scope,
            ccr.origin_character_id,
            ccr.character_instance_id,
            ccr.world_id,
            ccr.source_id,
            ccr.speaker_id,
            ccr.speaker_type,
            ccr.timeline_id,
            o.proposition_id
        FROM neighbor n
        JOIN LATERAL (
            SELECT o.claim_id, o.proposition_id
            FROM aios.observation o
            WHERE o.proposition_id=n.proposition_id
            ORDER BY o.observed_at DESC
            LIMIT 1
        ) o ON true
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
        ORDER BY n.similarity DESC
        LIMIT $3
        """,
        proposition_id,
        NEIGHBOR_MIN_SIMILARITY,
        NEIGHBOR_LIMIT,
    )
    return [dict(r) for r in rows]


def _candidate_allowed(
    *,
    current: dict[str, Any],
    candidate: SemanticOwnershipResolution,
    canonical_speaker_id: Optional[str],
) -> bool:
    if candidate.owner_kind in {"source", "unresolved"}:
        return False

    current_world = current.get("world_id")
    if candidate.owner_kind == "world":
        return bool(current_world and candidate.world_id == current_world)

    if candidate.owner_kind == "character":
        explicit_character = current.get("origin_character_id")
        permitted_character = str(explicit_character) if explicit_character else canonical_speaker_id
        return bool(
            permitted_character
            and candidate.character_id
            and _norm(candidate.character_id) == _norm(permitted_character)
        )

    return False


async def _resolve_from_neighbors(
    db: Database,
    row: dict[str, Any],
) -> Optional[SemanticOwnershipResolution]:
    proposition_id = row.get("proposition_id")
    if not proposition_id:
        return None

    if _norm(row.get("epistemic_scope")) == "speaker":
        return None

    canonical_speaker_id: Optional[str] = None
    if row.get("speaker_id"):
        identity = await resolve_character_mention(db, str(row["speaker_id"]))
        if identity:
            canonical_speaker_id = identity["character_id"]

    neighbors = await _neighbor_rows(db, proposition_id)
    scores: dict[str, float] = {}
    counts: dict[str, int] = {}
    decisions: dict[str, SemanticOwnershipResolution] = {}
    similarities: dict[str, list[float]] = {}

    for neighbor in neighbors:
        candidate = deterministic_semantic_ownership(neighbor)
        if not _candidate_allowed(
            current=row,
            candidate=candidate,
            canonical_speaker_id=canonical_speaker_id,
        ):
            continue

        similarity = float(neighbor["similarity"])
        if row.get("timeline_id") and neighbor.get("timeline_id") == row.get("timeline_id"):
            similarity = min(1.0, similarity + 0.03)

        key = candidate.owner_key
        scores[key] = scores.get(key, 0.0) + similarity
        counts[key] = counts.get(key, 0) + 1
        decisions[key] = candidate
        similarities.setdefault(key, []).append(float(neighbor["similarity"]))

    if not scores:
        return None

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_key, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    total_score = sum(scores.values())
    purity = best_score / total_score if total_score else 0.0
    margin = (best_score - second_score) / total_score if total_score else 0.0
    best_count = counts[best_key]

    if (
        best_count < NEIGHBOR_MIN_COUNT
        or purity < NEIGHBOR_MIN_PURITY
        or margin < NEIGHBOR_MIN_MARGIN
    ):
        return None

    winner = decisions[best_key]
    avg_similarity = sum(similarities[best_key]) / len(similarities[best_key])
    return SemanticOwnershipResolution(
        owner_kind=winner.owner_kind,
        owner_key=winner.owner_key,
        status="resolved",
        confidence=min(0.97, max(0.0, 0.5 * purity + 0.5 * avg_similarity)),
        resolution_source="semantic_neighbor_vote",
        character_id=winner.character_id,
        character_instance_id=row.get("character_instance_id") if winner.owner_kind == "character" else None,
        world_id=winner.world_id,
        source_id=str(row["source_id"]) if row.get("source_id") else None,
        evidence={
            "neighbor_count": best_count,
            "purity": round(purity, 6),
            "margin": round(margin, 6),
            "average_similarity": round(avg_similarity, 6),
            "candidate_scores": {k: round(v, 6) for k, v in scores.items()},
        },
    )


async def resolve_semantic_ownership(
    db: Database,
    row: dict[str, Any],
) -> SemanticOwnershipResolution:
    """Hybrid ownership resolver: facts first, vector evidence only for ambiguity."""
    deterministic = deterministic_semantic_ownership(row)
    if deterministic.status == "resolved":
        return deterministic

    speaker = await _resolve_speaker_character(db, row)
    if speaker:
        return speaker

    neighbor = await _resolve_from_neighbors(db, row)
    if neighbor:
        return neighbor

    return deterministic
