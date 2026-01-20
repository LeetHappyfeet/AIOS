from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Set
from uuid import UUID

from aios_app.db import Database

logger = logging.getLogger("aios.world.split_seeder")


# ============================================================
# Parameters (tunable, conservative)
# ============================================================

MIN_CLUSTER_SIZE = 3
MIN_AVG_SIMILARITY = 0.72
MAX_PREDICATE_VARIETY = 1  # >1 indicates tension


# ============================================================
# Models
# ============================================================

@dataclass
class ClaimLite:
    claim_id: UUID
    subject: str | None
    predicate: str | None
    object: str | None


# ============================================================
# Helpers
# ============================================================

def _connected_components(edges: List[tuple[UUID, UUID]]) -> List[Set[UUID]]:
    graph: Dict[UUID, Set[UUID]] = defaultdict(set)
    for a, b in edges:
        graph[a].add(b)
        graph[b].add(a)

    seen: Set[UUID] = set()
    components: List[Set[UUID]] = []

    for node in graph:
        if node in seen:
            continue
        stack = [node]
        comp = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            comp.add(cur)
            stack.extend(graph[cur])
        components.append(comp)

    return components


# ============================================================
# Public API
# ============================================================

async def seed_world_splits_from_similarity(
    db: Database,
    *,
    similarity_threshold: float = 0.70,
    limit: int = 500,
) -> int:
    """
    Seed world split candidates from similarity clusters.

    Returns number of world_split_candidate rows inserted.
    """

    edges = await db.fetch(
        """
        SELECT claim_a_id, claim_b_id, similarity
        FROM aios.claim_similarity_edge
        WHERE similarity >= $1
        ORDER BY similarity DESC
        LIMIT $2
        """,
        similarity_threshold,
        limit,
    )

    if not edges:
        return 0

    edge_pairs = [(e["claim_a_id"], e["claim_b_id"]) for e in edges]
    components = _connected_components(edge_pairs)

    if not components:
        return 0

    # Fetch minimal claim info
    ids = {cid for comp in components for cid in comp}

    rows = await db.fetch(
        """
        SELECT claim_id, subject, predicate, object
        FROM aios.claim_candidate
        WHERE claim_id = ANY($1::uuid[])
        """,
        list(ids),
    )

    claims: Dict[UUID, ClaimLite] = {
        r["claim_id"]: ClaimLite(
            claim_id=r["claim_id"],
            subject=r["subject"],
            predicate=r["predicate"],
            object=r["object"],
        )
        for r in rows
    }

    inserted = 0

    for comp in components:
        if len(comp) < MIN_CLUSTER_SIZE:
            continue

        predicates = set()
        subjects = set()

        for cid in comp:
            c = claims.get(cid)
            if not c:
                continue
            if c.predicate:
                predicates.add(c.predicate.lower())
            if c.subject:
                subjects.add(c.subject.lower())

        # Heuristic tension signal
        if len(predicates) <= MAX_PREDICATE_VARIETY:
            continue

        # Create split candidate
        await db.execute(
            """
            INSERT INTO aios.world_split_candidate (
              seed_claim_ids,
              reason,
              signal_strength
            )
            VALUES ($1::uuid[], $2, $3)
            ON CONFLICT DO NOTHING
            """,
            list(comp),
            f"predicate_divergence:{len(predicates)}",
            min(1.0, len(predicates) / 3.0),
        )

        inserted += 1

    logger.info("Seeded %d world split candidates", inserted)
    return inserted
