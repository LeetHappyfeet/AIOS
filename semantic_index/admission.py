from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

from qdrant_client.http import models as qm

from aios_app.db import Database
from .config import SemanticIndexConfig
from .embeddings import Embedder
from .store import QdrantStore


@dataclass(frozen=True)
class AdmissionCandidate:
    proposition_id: UUID
    score: float
    topic_key: str | None
    claim_kind: str | None
    predicate_family: str | None
    subject_norm: str | None
    predicate_norm: str | None
    object_norm: str | None
    polarity: int
    modality: str | None


@dataclass(frozen=True)
class AdmissionDecision:
    decision: str
    candidate: AdmissionCandidate | None
    reason: str


def _scope_filter(scope: dict[str, Any]) -> qm.Filter | None:
    """Build a conservative Qdrant filter for a resolved epistemic scope.

    Proposition vectors are canonical and therefore are not owned by a scope.
    Scope is used only when a caller explicitly asks to restrict legacy payloads;
    semantic correctness is checked against PostgreSQL after ANN lookup.
    """
    must: list[qm.FieldCondition] = []
    if scope.get("world_id"):
        must.append(qm.FieldCondition(key="world_ids", match=qm.MatchAny(any=[str(scope["world_id"])])))
    if scope.get("character_instance_id"):
        must.append(qm.FieldCondition(key="instance_ids", match=qm.MatchAny(any=[str(scope["character_instance_id"])])))
    return qm.Filter(must=must) if must else None


def _payload_candidate(payload: dict[str, Any], score: float) -> AdmissionCandidate | None:
    proposition_id = payload.get("proposition_id")
    if not proposition_id:
        return None
    try:
        pid = UUID(str(proposition_id))
    except (TypeError, ValueError):
        return None
    return AdmissionCandidate(
        proposition_id=pid,
        score=float(score),
        topic_key=payload.get("topic_key"),
        claim_kind=payload.get("claim_kind"),
        predicate_family=payload.get("predicate_family"),
        subject_norm=payload.get("subject_norm"),
        predicate_norm=payload.get("predicate_norm"),
        object_norm=payload.get("object_norm"),
        polarity=int(payload.get("polarity", 1)),
        modality=payload.get("modality"),
    )


def search_canonical_candidates(
    *,
    cfg: SemanticIndexConfig,
    embedder: Embedder,
    store: QdrantStore,
    canonical_text: str,
    limit: int = 12,
) -> list[AdmissionCandidate]:
    """Find a tiny canonical proposition neighborhood for admission.

    This is intentionally candidate generation, not a truth decision.  The ANN
    result is always checked structurally before it may suppress downstream work.
    """
    vector = embedder.embed([canonical_text])[0]
    result = store.client.query_points(
        collection_name=cfg.proposition_collection,
        query=vector,
        limit=max(1, int(limit)),
        with_payload=True,
        with_vectors=False,
    )
    points: Iterable[Any] = getattr(result, "points", result)
    candidates: list[AdmissionCandidate] = []
    for point in points:
        candidate = _payload_candidate(dict(point.payload or {}), float(point.score))
        if candidate is not None:
            candidates.append(candidate)
    return candidates


async def classify_semantic_admission(
    db: Database,
    *,
    proposition_id: UUID,
    candidates: Iterable[AdmissionCandidate],
    reinforce_score: float = 0.94,
    challenge_score: float = 0.90,
) -> AdmissionDecision:
    """Conservatively classify ANN candidates using authoritative SQL semantics.

    Exact proposition identity is handled by the cheaper SQL/hash gate.  This
    function only handles near-neighbor admission.  It deliberately refuses to
    suppress when identity, polarity, modality, or predicate semantics are not
    strong enough.
    """
    current = await db.fetchrow(
        """
        SELECT proposition_id, subject_norm, predicate_norm, object_norm,
               polarity, modality, topic_key
        FROM aios.proposition
        WHERE proposition_id=$1
        """,
        proposition_id,
    )
    if not current:
        return AdmissionDecision("novel", None, "missing_current_proposition")

    for candidate in sorted(candidates, key=lambda c: c.score, reverse=True):
        if candidate.proposition_id == proposition_id:
            continue
        other = await db.fetchrow(
            """
            SELECT p.subject_norm, p.predicate_norm, p.object_norm,
                   p.polarity, p.modality, p.topic_key,
                   ctx.claim_kind, ctx.predicate_family
            FROM aios.proposition p
            LEFT JOIN LATERAL (
                SELECT ccr.claim_kind, ccr.predicate_family
                FROM aios.observation o
                JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
                WHERE o.proposition_id=p.proposition_id
                ORDER BY ccr.resolved_at DESC
                LIMIT 1
            ) ctx ON true
            WHERE p.proposition_id=$1
            """,
            candidate.proposition_id,
        )
        if not other:
            continue

        same_subject = bool(current["subject_norm"] and current["subject_norm"] == other["subject_norm"])
        same_predicate = bool(current["predicate_norm"] and current["predicate_norm"] == other["predicate_norm"])
        same_object = bool(current["object_norm"] and current["object_norm"] == other["object_norm"])
        same_modality = (current["modality"] or "asserted") == (other["modality"] or "asserted")
        same_polarity = int(current["polarity"]) == int(other["polarity"])

        if same_subject and same_predicate and same_object and same_modality:
            if not same_polarity and candidate.score >= challenge_score:
                return AdmissionDecision("challenges", candidate, "same_spo_opposite_polarity")
            if same_polarity and candidate.score >= reinforce_score:
                return AdmissionDecision("reinforces", candidate, "same_spo_same_polarity_near_neighbor")

        # Topic/predicate-family similarity is useful evidence for a refinement,
        # but never sufficient to suppress semantic expansion.
        same_topic = bool(current["topic_key"] and current["topic_key"] == other["topic_key"])
        same_family = bool(candidate.predicate_family and candidate.predicate_family == other["predicate_family"])
        if candidate.score >= challenge_score and (same_topic or same_family):
            return AdmissionDecision("refines", candidate, "related_semantic_neighbor")

    return AdmissionDecision("novel", None, "no_safe_semantic_neighbor")
