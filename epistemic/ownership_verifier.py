from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from aios_app.db import Database
from aios_app.epistemic.ownership import (
    SemanticOwnershipResolution,
    deterministic_semantic_ownership,
    resolve_character_mention,
)

VERIFIER_VERSION = "semantic-ownership-adversarial-v1"
MIN_ACCEPT_SCORE = 4
MIN_ACCEPT_MARGIN = 3
MIN_STABILITY = 0.75
NEIGHBOR_MIN_SIMILARITY = 0.76
NEIGHBOR_LIMIT = 16
PROTECTED_RESOLUTION_SOURCES = {
    "explicit_character_scope",
    "explicit_narrative_world_scope",
    "explicit_source_scope",
    "explicit_world_scope",
}


@dataclass(frozen=True)
class OwnershipCandidate:
    owner_kind: str
    owner_key: str
    character_id: Optional[str] = None
    world_id: Any = None
    source_id: Optional[str] = None


@dataclass(frozen=True)
class AdversarialVerification:
    status: str
    proposed_owner_key: str
    winner_owner_key: Optional[str]
    winner_score: int
    runner_up_score: int
    margin: int
    stability: float
    matrix: dict[str, dict[str, int]] = field(default_factory=dict)
    vector_evidence: dict[str, Any] = field(default_factory=dict)

    def as_meta(self) -> dict[str, Any]:
        return {
            "verifier_version": VERIFIER_VERSION,
            "status": self.status,
            "proposed_owner_key": self.proposed_owner_key,
            "winner_owner_key": self.winner_owner_key,
            "winner_score": self.winner_score,
            "runner_up_score": self.runner_up_score,
            "margin": self.margin,
            "stability": round(float(self.stability), 6),
            "matrix": self.matrix,
            "vector_evidence": self.vector_evidence,
        }


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _candidate_from_resolution(resolution: SemanticOwnershipResolution) -> OwnershipCandidate:
    return OwnershipCandidate(
        owner_kind=resolution.owner_kind,
        owner_key=resolution.owner_key,
        character_id=resolution.character_id,
        world_id=resolution.world_id,
        source_id=resolution.source_id,
    )


def _candidate_key(candidate: OwnershipCandidate) -> str:
    return candidate.owner_key


def _explicit_scope_score(row: dict[str, Any], candidate: OwnershipCandidate) -> int:
    scope = _norm(row.get("epistemic_scope"))
    if scope == "character":
        return 3 if candidate.owner_kind == "character" else -2
    if scope in {"narrative", "world", "observation"}:
        return 3 if candidate.owner_kind == "world" else -2
    if scope == "source":
        return 3 if candidate.owner_kind == "source" else -3
    if scope == "speaker":
        return 1 if candidate.owner_kind == "character" else 0
    return 0


def _identity_score(
    row: dict[str, Any],
    candidate: OwnershipCandidate,
    canonical_speaker_id: Optional[str],
) -> int:
    if candidate.owner_kind != "character":
        return 0
    explicit = row.get("origin_character_id")
    expected = str(explicit) if explicit else canonical_speaker_id
    if not expected:
        return 0
    if candidate.character_id and _norm(candidate.character_id) == _norm(expected):
        return 3
    return -3


def _world_score(row: dict[str, Any], candidate: OwnershipCandidate) -> int:
    current_world = row.get("world_id")
    if candidate.owner_kind == "world":
        if not current_world or not candidate.world_id:
            return -2
        return 2 if candidate.world_id == current_world else -3
    if candidate.owner_kind == "character" and current_world:
        if candidate.world_id and candidate.world_id != current_world:
            return -3
        return 1
    return 0


def _counterfactual_score(row: dict[str, Any], candidate: OwnershipCandidate) -> int:
    claim_kind = _norm(row.get("claim_kind")).upper()
    family = _norm(row.get("predicate_family")).upper()

    epistemic = family in {"EPISTEMIC", "MEMORY"} or claim_kind in {"BELIEF", "MEMORY"}
    observable = (
        claim_kind == "EVENT"
        or family in {"ACTION", "CAUSAL", "SPATIAL", "TEMPORAL"}
    )

    if epistemic:
        if candidate.owner_kind == "character":
            return 2
        if candidate.owner_kind == "world":
            return -2
    if observable:
        if candidate.owner_kind == "world":
            return 2
        if candidate.owner_kind == "character":
            return -1
    return 0


def _vector_scores(
    candidates: list[OwnershipCandidate],
    vector_totals: dict[str, float],
) -> dict[str, int]:
    result = {candidate.owner_key: 0 for candidate in candidates}
    ranked = sorted(
        ((key, score) for key, score in vector_totals.items() if key in result),
        key=lambda item: item[1],
        reverse=True,
    )
    if not ranked or ranked[0][1] <= 0:
        return result

    best_key, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    total = sum(score for _, score in ranked)
    margin = (best_score - second_score) / total if total else 0.0
    result[best_key] = 2 if margin >= 0.15 else 1

    for key, score in ranked[1:]:
        if score > 0 and best_score > score:
            result[key] = -1 if margin >= 0.15 else 0
    return result


def build_adversarial_matrix(
    row: dict[str, Any],
    candidates: list[OwnershipCandidate],
    *,
    canonical_speaker_id: Optional[str] = None,
    vector_totals: Optional[dict[str, float]] = None,
) -> dict[str, dict[str, int]]:
    vector_totals = vector_totals or {}
    vector_axis = _vector_scores(candidates, vector_totals)
    matrix: dict[str, dict[str, int]] = {}
    for candidate in candidates:
        matrix[candidate.owner_key] = {
            "scope": _explicit_scope_score(row, candidate),
            "identity": _identity_score(row, candidate, canonical_speaker_id),
            "world": _world_score(row, candidate),
            "vector": vector_axis.get(candidate.owner_key, 0),
            "counterfactual": _counterfactual_score(row, candidate),
        }
    return matrix


def _totals(matrix: dict[str, dict[str, int]], omit_axis: Optional[str] = None) -> dict[str, int]:
    return {
        key: sum(score for axis, score in axes.items() if axis != omit_axis)
        for key, axes in matrix.items()
    }


def _rank(totals: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def score_adversarial_matrix(
    matrix: dict[str, dict[str, int]],
    *,
    proposed_owner_key: str,
) -> AdversarialVerification:
    totals = _totals(matrix)
    ranked = _rank(totals)
    if not ranked:
        return AdversarialVerification(
            status="insufficient_evidence",
            proposed_owner_key=proposed_owner_key,
            winner_owner_key=None,
            winner_score=0,
            runner_up_score=0,
            margin=0,
            stability=0.0,
            matrix=matrix,
        )

    winner_key, winner_score = ranked[0]
    runner_score = ranked[1][1] if len(ranked) > 1 else 0
    margin = winner_score - runner_score

    axes = sorted({axis for values in matrix.values() for axis in values})
    stable_trials = 0
    for axis in axes:
        trial = _rank(_totals(matrix, omit_axis=axis))
        if trial and trial[0][0] == winner_key:
            stable_trials += 1
    stability = stable_trials / len(axes) if axes else 0.0

    if winner_key != proposed_owner_key:
        status = "challenged"
    elif winner_score < MIN_ACCEPT_SCORE or margin < MIN_ACCEPT_MARGIN or stability < MIN_STABILITY:
        status = "fragile"
    else:
        status = "verified"

    return AdversarialVerification(
        status=status,
        proposed_owner_key=proposed_owner_key,
        winner_owner_key=winner_key,
        winner_score=winner_score,
        runner_up_score=runner_score,
        margin=margin,
        stability=stability,
        matrix=matrix,
    )


async def _neighbor_owner_totals(
    db: Database,
    row: dict[str, Any],
    candidates: list[OwnershipCandidate],
) -> tuple[dict[str, float], dict[str, Any]]:
    proposition_id = row.get("proposition_id")
    if not proposition_id:
        return {}, {"neighbor_count": 0}

    candidate_keys = {candidate.owner_key for candidate in candidates}
    rows = await db.fetch(
        """
        WITH raw_neighbor AS (
            SELECT neighbor_proposition_id AS proposition_id, similarity
            FROM aios.semantic_neighbor_candidate
            WHERE proposition_id=$1 AND similarity >= $2
            UNION ALL
            SELECT proposition_id, similarity
            FROM aios.semantic_neighbor_candidate
            WHERE neighbor_proposition_id=$1 AND similarity >= $2
        ), neighbor AS (
            SELECT proposition_id, MAX(similarity) AS similarity
            FROM raw_neighbor
            GROUP BY proposition_id
        )
        SELECT n.similarity, ccr.*
        FROM neighbor n
        JOIN LATERAL (
            SELECT o.claim_id
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

    totals: dict[str, float] = {}
    accepted = 0
    for neighbor_row in rows:
        neighbor = dict(neighbor_row)
        resolution = deterministic_semantic_ownership(neighbor)
        if resolution.status != "resolved" or resolution.owner_key not in candidate_keys:
            continue
        if row.get("world_id") and resolution.world_id and resolution.world_id != row.get("world_id"):
            continue
        totals[resolution.owner_key] = totals.get(resolution.owner_key, 0.0) + float(neighbor["similarity"])
        accepted += 1

    return totals, {
        "neighbor_count": len(rows),
        "accepted_neighbor_count": accepted,
        "owner_similarity_totals": {key: round(value, 6) for key, value in totals.items()},
    }


async def _plausible_candidates(
    db: Database,
    row: dict[str, Any],
    proposed: SemanticOwnershipResolution,
) -> tuple[list[OwnershipCandidate], Optional[str]]:
    candidates: dict[str, OwnershipCandidate] = {}
    proposed_candidate = _candidate_from_resolution(proposed)
    if proposed.owner_kind != "unresolved":
        candidates[proposed.owner_key] = proposed_candidate

    canonical_speaker_id: Optional[str] = None
    if row.get("speaker_id"):
        identity = await resolve_character_mention(db, str(row["speaker_id"]))
        if identity:
            canonical_speaker_id = identity["character_id"]

    character_id = row.get("origin_character_id") or canonical_speaker_id
    if character_id:
        key = f"char:{character_id}"
        candidates.setdefault(
            key,
            OwnershipCandidate(
                owner_kind="character",
                owner_key=key,
                character_id=str(character_id),
                world_id=row.get("world_id"),
            ),
        )

    if row.get("world_id"):
        world_id = row["world_id"]
        key = f"world:{world_id}:observed"
        candidates.setdefault(
            key,
            OwnershipCandidate(owner_kind="world", owner_key=key, world_id=world_id),
        )

    if _norm(row.get("epistemic_scope")) == "source" and row.get("source_id"):
        key = f"source:{row['source_id']}"
        candidates.setdefault(
            key,
            OwnershipCandidate(owner_kind="source", owner_key=key, source_id=str(row["source_id"])),
        )

    return list(candidates.values()), canonical_speaker_id


def _unresolved_from(
    row: dict[str, Any],
    proposed: SemanticOwnershipResolution,
    verification: AdversarialVerification,
) -> SemanticOwnershipResolution:
    anchor = row.get("claim_id") or row.get("proposition_id") or row.get("timeline_id") or "unknown"
    return SemanticOwnershipResolution(
        owner_kind="unresolved",
        owner_key=f"unresolved:{anchor}",
        status="unresolved",
        confidence=0.0,
        resolution_source="adversarial_verifier_rejected",
        world_id=row.get("world_id"),
        source_id=str(row["source_id"]) if row.get("source_id") else None,
        evidence={
            "proposed_resolution": proposed.as_meta(),
            "adversarial_verification": verification.as_meta(),
        },
    )


async def verify_semantic_ownership(
    db: Database,
    row: dict[str, Any],
    proposed: SemanticOwnershipResolution,
) -> SemanticOwnershipResolution:
    """Challenge an inferred owner against plausible alternatives before topology placement."""
    if proposed.status != "resolved":
        return proposed

    candidates, canonical_speaker_id = await _plausible_candidates(db, row, proposed)
    if len(candidates) < 2:
        verification = AdversarialVerification(
            status="single_plausible_owner",
            proposed_owner_key=proposed.owner_key,
            winner_owner_key=proposed.owner_key,
            winner_score=0,
            runner_up_score=0,
            margin=0,
            stability=1.0,
            matrix={},
        )
        return replace(
            proposed,
            evidence={**proposed.evidence, "adversarial_verification": verification.as_meta()},
        )

    vector_totals, vector_evidence = await _neighbor_owner_totals(db, row, candidates)
    matrix = build_adversarial_matrix(
        row,
        candidates,
        canonical_speaker_id=canonical_speaker_id,
        vector_totals=vector_totals,
    )
    verification = score_adversarial_matrix(matrix, proposed_owner_key=proposed.owner_key)
    verification = replace(verification, vector_evidence=vector_evidence)

    if proposed.resolution_source in PROTECTED_RESOLUTION_SOURCES:
        protected_status = replace(
            verification,
            status=("verified" if verification.status == "verified" else "protected_explicit"),
        )
        return replace(
            proposed,
            evidence={**proposed.evidence, "adversarial_verification": protected_status.as_meta()},
        )

    if verification.status != "verified":
        return _unresolved_from(row, proposed, verification)

    return replace(
        proposed,
        evidence={**proposed.evidence, "adversarial_verification": verification.as_meta()},
    )
