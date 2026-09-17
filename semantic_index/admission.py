from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

from aios_app.db import Database
from .config import SemanticIndexConfig
from .embeddings import Embedder
from .store import QdrantStore


@dataclass(frozen=True)
class AdmissionCandidate:
    proposition_id: UUID
    score: float


@dataclass(frozen=True)
class AdmissionDecision:
    decision: str
    candidate: AdmissionCandidate | None
    matched_claim_id: UUID | None
    reason: str


def _payload_candidate(payload: dict[str, Any], score: float) -> AdmissionCandidate | None:
    proposition_id = payload.get("proposition_id")
    if not proposition_id:
        return None
    try:
        return AdmissionCandidate(UUID(str(proposition_id)), float(score))
    except (TypeError, ValueError):
        return None


def search_canonical_candidates(*, cfg: SemanticIndexConfig, embedder: Embedder,
                                store: QdrantStore, canonical_text: str,
                                limit: int = 12) -> list[AdmissionCandidate]:
    """Generate a small ANN neighborhood; never make truth decisions here."""
    vector = embedder.embed([canonical_text])[0]
    result = store.client.query_points(
        collection_name=cfg.proposition_collection, query=vector,
        limit=max(1, int(limit)), with_payload=True, with_vectors=False,
    )
    points: Iterable[Any] = getattr(result, "points", result)
    candidates: list[AdmissionCandidate] = []
    for point in points:
        candidate = _payload_candidate(dict(point.payload or {}), float(point.score))
        if candidate is not None:
            candidates.append(candidate)
    return candidates


async def _same_scope_claim(db: Database, *, proposition_id: UUID, scope_key: str) -> UUID | None:
    row = await db.fetchrow(
        """
        SELECT o.claim_id
        FROM aios.observation o
        WHERE o.proposition_id=$1
          AND aios.exact_admission_scope_key(o.claim_id)=$2
        ORDER BY o.observed_at, o.observation_id
        LIMIT 1
        """, proposition_id, scope_key,
    )
    return row["claim_id"] if row else None


async def classify_semantic_admission(db: Database, *, proposition_id: UUID,
                                      scope_key: str,
                                      candidates: Iterable[AdmissionCandidate],
                                      reinforce_score: float = 0.94,
                                      challenge_score: float = 0.90) -> AdmissionDecision:
    """Verify Qdrant neighbors against authoritative SQL and epistemic scope."""
    current = await db.fetchrow(
        """SELECT subject_norm,predicate_norm,object_norm,polarity,modality,topic_key
           FROM aios.proposition WHERE proposition_id=$1""", proposition_id,
    )
    if not current:
        return AdmissionDecision("novel", None, None, "missing_current_proposition")

    for candidate in sorted(candidates, key=lambda c: c.score, reverse=True):
        if candidate.proposition_id == proposition_id:
            continue
        matched_claim_id = await _same_scope_claim(
            db, proposition_id=candidate.proposition_id, scope_key=scope_key,
        )
        if matched_claim_id is None:
            continue
        other = await db.fetchrow(
            """SELECT subject_norm,predicate_norm,object_norm,polarity,modality,topic_key
               FROM aios.proposition WHERE proposition_id=$1""", candidate.proposition_id,
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
                return AdmissionDecision("challenges", candidate, matched_claim_id, "same_spo_opposite_polarity")
            if same_polarity and candidate.score >= reinforce_score:
                return AdmissionDecision("reinforces", candidate, matched_claim_id, "same_spo_same_polarity_near_neighbor")
        same_topic = bool(current["topic_key"] and current["topic_key"] == other["topic_key"])
        if candidate.score >= challenge_score and same_topic:
            return AdmissionDecision("refines", candidate, matched_claim_id, "same_topic_semantic_neighbor")
    return AdmissionDecision("novel", None, None, "no_safe_same_scope_neighbor")


async def _record_decision(db: Database, *, claim_id: UUID, proposition_id: UUID,
                           scope_key: str, decision: AdmissionDecision) -> None:
    candidate = decision.candidate
    await db.execute(
        """
        INSERT INTO aios.semantic_neighbor_admission (
            claim_id,proposition_id,matched_proposition_id,matched_claim_id,
            scope_key,decision,similarity,reason,gate_version,
            evidence_preserved,decided_at,meta
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'qdrant-admission-v1',true,now(),$9::jsonb)
        ON CONFLICT (claim_id) DO UPDATE SET
            proposition_id=EXCLUDED.proposition_id,
            matched_proposition_id=EXCLUDED.matched_proposition_id,
            matched_claim_id=EXCLUDED.matched_claim_id,scope_key=EXCLUDED.scope_key,
            decision=EXCLUDED.decision,similarity=EXCLUDED.similarity,
            reason=EXCLUDED.reason,gate_version=EXCLUDED.gate_version,
            evidence_preserved=true,decided_at=now(),meta=EXCLUDED.meta
        """, claim_id, proposition_id,
        candidate.proposition_id if candidate else None, decision.matched_claim_id,
        scope_key, decision.decision, candidate.score if candidate else None,
        decision.reason, '{"candidate_source":"qdrant_canonical_proposition"}',
    )
    if decision.decision != "reinforces":
        return
    await db.execute(
        """
        INSERT INTO aios.rdf_promotion_log (
            claim_id,rdf_dataset,rdf_graph,rdf_subject,rdf_predicate,
            rdf_object,promoted_by,promoted_at,promotion_meta
        ) VALUES ($1,'world','urn:aios:world:epistemic',
            'urn:aios:world:observation:' || $1::text,'world:observesProposition',
            'suppressed:semantic-reinforcement','qdrant_semantic_admission_gate',now(),
            jsonb_build_object('suppressed',true,'reason',$2,'matched_claim_id',$3,
                'matched_proposition_id',$4,'evidence_preserved',true,
                'gate_version','qdrant-admission-v1'))
        ON CONFLICT (claim_id,rdf_dataset,rdf_graph,rdf_predicate)
        DO UPDATE SET rdf_object=EXCLUDED.rdf_object,promoted_by=EXCLUDED.promoted_by,
                      promoted_at=EXCLUDED.promoted_at,promotion_meta=EXCLUDED.promotion_meta
        """, claim_id, decision.reason, decision.matched_claim_id,
        candidate.proposition_id if candidate else None,
    )
    await db.execute(
        """
        INSERT INTO aios.semantic_topology_projection (
            projection_key,claim_id,scope_key,rdf_dataset,rdf_graph,
            resolver_version,projected_at,last_error,meta
        ) VALUES ('claim:' || $1::text || ':semantic-reinforcement',$1,$2,'none',
            'urn:aios:suppressed:semantic-reinforcement','semantic-topology-v1',now(),NULL,
            jsonb_build_object('suppressed',true,'reason',$3,'matched_claim_id',$4,
                'matched_proposition_id',$5,'evidence_preserved',true,
                'gate_version','qdrant-admission-v1'))
        ON CONFLICT (projection_key) DO UPDATE SET
            projected_at=EXCLUDED.projected_at,last_error=NULL,
            meta=EXCLUDED.meta,updated_at=now()
        """, claim_id, scope_key, decision.reason, decision.matched_claim_id,
        candidate.proposition_id if candidate else None,
    )


async def fail_open_stalled_admissions_once(db: Database, cfg: SemanticIndexConfig,
                                             *, timeout_seconds: int = 120) -> int:
    """Release exact-novel claims that vector admission has failed to classify.

    A Qdrant/index outage is allowed to cost optimization, never memory. Claims
    older than the timeout receive a terminal bypass decision and may expand.
    """
    rows = await db.fetch(
        """
        SELECT sea.claim_id,sea.proposition_id,sea.scope_key
        FROM aios.semantic_exact_admission sea
        JOIN aios.observation o ON o.claim_id=sea.claim_id
        WHERE sea.decision='novel_exact'
          AND sea.decided_at < now() - make_interval(secs => $2)
          AND NOT EXISTS (
              SELECT 1 FROM aios.semantic_neighbor_admission sna
              WHERE sna.claim_id=sea.claim_id
          )
        ORDER BY sea.decided_at
        LIMIT $1
        """, cfg.batch_size, timeout_seconds,
    )
    for row in rows:
        await _record_decision(
            db, claim_id=row["claim_id"], proposition_id=row["proposition_id"],
            scope_key=row["scope_key"],
            decision=AdmissionDecision("bypass_timeout", None, None,
                                       "semantic_admission_timeout_fail_open"),
        )
    return len(rows)


async def admission_backlog_snapshot(db: Database, *, timeout_seconds: int = 120) -> dict[str, int]:
    row = await db.fetchrow(
        """
        SELECT
          count(*) FILTER (WHERE sna.claim_id IS NULL) AS pending,
          count(*) FILTER (WHERE sna.claim_id IS NULL AND sea.decided_at < now()-make_interval(secs => $1)) AS overdue
        FROM aios.semantic_exact_admission sea
        LEFT JOIN aios.semantic_neighbor_admission sna ON sna.claim_id=sea.claim_id
        WHERE sea.decision='novel_exact'
        """, timeout_seconds,
    )
    return {"pending": int(row["pending"] or 0), "overdue": int(row["overdue"] or 0)} if row else {"pending": 0, "overdue": 0}


async def admit_semantic_neighbors_once(db: Database, cfg: SemanticIndexConfig, *,
                                        embedder: Embedder, store: QdrantStore) -> int:
    """Classify normalized exact-novel observations after proposition indexing."""
    rows = await db.fetch(
        """
        SELECT o.claim_id,o.proposition_id,p.canonical_text,
               aios.exact_admission_scope_key(o.claim_id) AS scope_key
        FROM aios.observation o
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        JOIN aios.semantic_exact_admission sea ON sea.claim_id=o.claim_id
        JOIN aios.semantic_vector_index_state svi
          ON svi.object_type='proposition' AND svi.object_key=o.proposition_id::text
         AND svi.qdrant_collection=$2 AND svi.embedding_model=$3 AND svi.embedding_version=$4
        WHERE sea.decision='novel_exact'
          AND NOT EXISTS (SELECT 1 FROM aios.semantic_neighbor_admission sna WHERE sna.claim_id=o.claim_id)
        ORDER BY o.observed_at LIMIT $1
        """, cfg.batch_size, cfg.proposition_collection,
        cfg.embedding_model, cfg.embedding_version,
    )
    processed = 0
    for row in rows:
        scope_key = row["scope_key"]
        if not scope_key:
            continue
        try:
            candidates = search_canonical_candidates(
                cfg=cfg, embedder=embedder, store=store, canonical_text=row["canonical_text"],
            )
            decision = await classify_semantic_admission(
                db, proposition_id=row["proposition_id"], scope_key=scope_key,
                candidates=candidates,
            )
        except Exception as exc:
            # Do not suppress on vector/index errors. Record a terminal expansion
            # decision so the scheduler cannot strand the claim.
            decision = AdmissionDecision("bypass_error", None, None,
                                         f"semantic_admission_error:{type(exc).__name__}")
        await _record_decision(
            db, claim_id=row["claim_id"], proposition_id=row["proposition_id"],
            scope_key=scope_key, decision=decision,
        )
        processed += 1
    return processed
