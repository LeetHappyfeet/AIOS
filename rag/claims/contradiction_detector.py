from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
from uuid import UUID

from aios_app.db import Database
from ..rag_config import RagConfig

logger = logging.getLogger("aios.rag.claim_contradiction")

# ============================================================
# Detector identity
# ============================================================

DETECTOR_VERSION = "v2-edge-driven-polarity"

DEFAULT_DETECTOR_CONF = {
    "source": "claim_similarity_edge",
    "signals": ["negation", "antonym", "spo_overlap"],
}


# ============================================================
# Models
# ============================================================

@dataclass(frozen=True)
class ClaimText:
    claim_id: UUID
    sentence_id: UUID
    subject: str | None
    predicate: str | None
    object: str | None
    raw_text: str
    extraction_ver: str
    status: str


# ============================================================
# Heuristics (unchanged logic)
# ============================================================

_NEGATION_RE = re.compile(
    r"\b(not|no|never|none|n't|cannot|can't|won't|without|deny|denies|denied|false)\b",
    re.IGNORECASE,
)

_ANTONYM_PAIRS = {
    "increase": "decrease",
    "increased": "decreased",
    "more": "less",
    "higher": "lower",
    "before": "after",
    "true": "false",
    "exists": "does not exist",
}

_STOPWORDS = {
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "do", "does", "did",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _tokenize(s: str) -> List[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", _norm(s))
        if t and t not in _STOPWORDS
    ]


def _has_negation(text: str) -> bool:
    return bool(_NEGATION_RE.search(text))


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa or sb else 0.0


def _antonym_hit(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    for x, y in _ANTONYM_PAIRS.items():
        if x in a and y in b:
            return True
        if y in a and x in b:
            return True
    return False


def _spo_text(c: ClaimText) -> str:
    if c.subject and c.predicate and c.object:
        return f"{c.subject} {c.predicate} {c.object}"
    return c.raw_text


def _score_contradiction(
    a: ClaimText,
    b: ClaimText,
    similarity: float,
) -> Tuple[float, List[str]]:
    reasons: List[str] = []

    if similarity < 0.70:
        return 0.0, ["similarity_too_low"]

    a_text, b_text = _spo_text(a), _spo_text(b)

    overlap = _jaccard(_tokenize(a_text), _tokenize(b_text))
    if overlap >= 0.55:
        reasons.append(f"high_token_overlap={overlap:.2f}")
    elif overlap >= 0.40:
        reasons.append(f"medium_token_overlap={overlap:.2f}")
    else:
        reasons.append(f"low_token_overlap={overlap:.2f}")

    neg_flip = _has_negation(a_text) != _has_negation(b_text)
    if neg_flip:
        reasons.append("negation_flip")

    antonym = _antonym_hit(a_text, b_text)
    if antonym:
        reasons.append("antonym_signal")

    score = min(0.40, max(0.0, (similarity - 0.70) * 0.8))

    if overlap >= 0.55:
        score += 0.25
    elif overlap >= 0.40:
        score += 0.15

    if neg_flip:
        score += 0.35
    if antonym:
        score += 0.15

    if not (neg_flip or antonym):
        score = min(score, 0.45)

    return max(0.0, min(1.0, score)), reasons


# ============================================================
# DB helpers
# ============================================================

async def _fetch_claims(db: Database, ids: List[UUID]) -> Dict[UUID, ClaimText]:
    rows = await db.fetch(
        """
        SELECT claim_id, sentence_id, subject, predicate, object,
               raw_text, extraction_ver, status
        FROM aios.claim_candidate
        WHERE claim_id = ANY($1::uuid[])
        """,
        ids,
    )

    return {
        r["claim_id"]: ClaimText(
            claim_id=r["claim_id"],
            sentence_id=r["sentence_id"],
            subject=r["subject"],
            predicate=r["predicate"],
            object=r["object"],
            raw_text=r["raw_text"],
            extraction_ver=r["extraction_ver"],
            status=r["status"],
        )
        for r in rows
    }


# ============================================================
# Public API (EDGE-DRIVEN)
# ============================================================

async def detect_contradictions_from_edges(
    db: Database,
    cfg: RagConfig,
    *,
    min_similarity: float = 0.75,
    min_contradiction_score: float = 0.70,
    limit: int = 500,
) -> int:
    """
    Detect contradictions by scanning claim_similarity_edge.

    Returns number of contradiction candidates inserted.
    """

    edges = await db.fetch(
        """
        SELECT claim_a_id, claim_b_id, similarity
        FROM aios.claim_similarity_edge
        WHERE similarity >= $1
        ORDER BY similarity DESC
        LIMIT $2
        """,
        min_similarity,
        limit,
    )

    if not edges:
        return 0

    ids: List[UUID] = []
    for e in edges:
        ids.append(e["claim_a_id"])
        ids.append(e["claim_b_id"])

    claims = await _fetch_claims(db, list(set(ids)))

    inserted = 0

    for e in edges:
        a = claims.get(e["claim_a_id"])
        b = claims.get(e["claim_b_id"])
        if not a or not b:
            continue

        score, reasons = _score_contradiction(a, b, e["similarity"])
        if score < min_contradiction_score:
            continue

        await db.execute(
            """
            INSERT INTO aios.claim_contradiction_candidate (
              claim_a_id,
              claim_b_id,
              similarity,
              contradiction_score,
              reasons,
              detector_ver,
              detector_conf
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb)
            ON CONFLICT (claim_a_id, claim_b_id) DO NOTHING
            """,
            e["claim_a_id"],
            e["claim_b_id"],
            e["similarity"],
            score,
            reasons,
            DETECTOR_VERSION,
            DEFAULT_DETECTOR_CONF,
        )

        inserted += 1

    logger.info("Inserted %d contradiction candidates (edge-driven)", inserted)
    return inserted
