from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List
from uuid import UUID

from qdrant_client.http import models as qm

from aios_app.db import Database
from aios_app.rag.rag_config import RagConfig
from aios_app.rag.qdrant_store import QdrantStore

logger = logging.getLogger("aios.world.claim_affinity")

# ============================================================
# Tunables
# ============================================================

MAX_WORLDS_PER_RUN = 20
MAX_CLAIMS_PER_WORLD = 200
MIN_SIMILARITY = 0.55


# ============================================================
# Models
# ============================================================

@dataclass
class WorldRow:
    world_id: UUID
    world_key: str
    bias_claim_ids: List[str]
    confidence: float


# ============================================================
# Main worker
# ============================================================

async def assign_claim_affinities_once(
    db: Database,
    cfg: RagConfig,
) -> int:
    """
    Assign probabilistic claim affinities to tentative worlds.

    Logic:
      - For each tentative world
      - Use its bias_claim_ids as anchors
      - Find nearby claims via vector similarity
      - Insert weighted affinities

    Returns number of affinities written.
    """

    worlds = await db.fetch(
        """
        SELECT
            w.world_id,
            w.world_key,
            (w.meta -> 'bias_claim_ids') AS bias_claim_ids,
            COALESCE((w.meta ->> 'confidence')::float, 0.5) AS confidence
        FROM aios.world w
        WHERE w.world_type = 'tentative'
        ORDER BY w.created_at
        LIMIT $1
        """,
        MAX_WORLDS_PER_RUN,
    )

    if not worlds:
        return 0

    store = QdrantStore(
        url=cfg.qdrant_url,
        api_key=cfg.qdrant_api_key,
        collection=cfg.qdrant_collection,
        vector_dim=cfg.embedding_dim,  # safe: already created
    )

    total_written = 0

    for r in worlds:
        world = WorldRow(
            world_id=r["world_id"],
            world_key=r["world_key"],
            bias_claim_ids=list(r["bias_claim_ids"] or []),
            confidence=float(r["confidence"]),
        )

        if not world.bias_claim_ids:
            continue

        # ----------------------------------------------------
        # Fetch anchor vectors
        # ----------------------------------------------------
        anchor_vectors = []

        for claim_id in world.bias_claim_ids[:5]:
            try:
                pts = store.client.retrieve(
                    collection_name=cfg.qdrant_collection,
                    ids=[claim_id],
                    with_vectors=True,
                )
                if pts:
                    anchor_vectors.append(pts[0].vector)
            except Exception:
                continue

        if not anchor_vectors:
            continue

        # ----------------------------------------------------
        # Search nearby claims
        # ----------------------------------------------------
        seen: set[str] = set()
        affinities = []

        for vec in anchor_vectors:
            hits = store.search(
                vector=vec,
                limit=MAX_CLAIMS_PER_WORLD,
                filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="status",
                            match=qm.MatchValue(value="pending"),
                        )
                    ]
                ),
            )

            for h in hits:
                claim_id = h.payload.get("claim_id")
                if not claim_id or claim_id in seen:
                    continue
                if h.score < MIN_SIMILARITY:
                    continue

                seen.add(claim_id)

                weight = min(
                    1.0,
                    h.score * world.confidence,
                )

                affinities.append((claim_id, weight))

        # ----------------------------------------------------
        # Persist affinities (idempotent)
        # ----------------------------------------------------
        for claim_id, weight in affinities:
            await db.execute(
                """
                INSERT INTO aios.claim_world_affinity (
                    claim_id,
                    world_id,
                    affinity
                )
                VALUES ($1, $2, $3)
                ON CONFLICT (claim_id, world_id)
                DO UPDATE SET affinity = GREATEST(
                    aios.claim_world_affinity.affinity,
                    EXCLUDED.affinity
                )
                """,
                UUID(claim_id),
                world.world_id,
                weight,
            )

            total_written += 1

        logger.info(
            "World %s assigned %d claim affinities",
            world.world_key,
            len(affinities),
        )

    return total_written
