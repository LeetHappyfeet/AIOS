from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List
from uuid import UUID

from aios_app.db import Database
from aios_app.rag.rag_config import RagConfig

logger = logging.getLogger("aios.world.split_promoter")

# ============================================================
# Tunables
# ============================================================

MAX_SPLITS_PER_RUN = 10


# ============================================================
# Models
# ============================================================

@dataclass
class SplitRow:
    split_id: UUID
    seed_section_id: UUID
    cluster_a: List[str]
    cluster_b: List[str]
    centroid_distance: float


# ============================================================
# Main promotion worker
# ============================================================

async def promote_world_splits_once(
    db: Database,
    cfg: RagConfig,
) -> int:
    """
    Promote world_split_candidate rows into tentative world tracks.

    Promotion means:
      - create two new world rows (A / B)
      - attach provenance via world.meta
      - DO NOT assign claims yet

    Returns number of worlds created.
    """

    splits = await db.fetch(
        """
        SELECT
            wsc.split_id,
            wsc.seed_section_id,
            wsc.cluster_a,
            wsc.cluster_b,
            wsc.centroid_distance
        FROM aios.world_split_candidate wsc
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.world w
            WHERE w.meta ->> 'seed_split_id' = wsc.split_id::text
        )
        ORDER BY wsc.created_at
        LIMIT $1
        """,
        MAX_SPLITS_PER_RUN,
    )

    if not splits:
        return 0

    # Resolve parent world ("liminal")
    parent = await db.fetchrow(
        """
        SELECT world_id
        FROM aios.world
        WHERE world_key = 'liminal'
        """
    )

    parent_world_id = parent["world_id"] if parent else None

    created = 0

    for r in splits:
        split = SplitRow(
            split_id=r["split_id"],
            seed_section_id=r["seed_section_id"],
            cluster_a=r["cluster_a"],
            cluster_b=r["cluster_b"],
            centroid_distance=r["centroid_distance"],
        )

        world_a = await _create_world(
            db,
            world_key=f"split:{split.split_id}:A",
            parent_world_id=parent_world_id,
            split_id=split.split_id,
            bias_claim_ids=split.cluster_a,
            confidence=split.centroid_distance,
        )

        world_b = await _create_world(
            db,
            world_key=f"split:{split.split_id}:B",
            parent_world_id=parent_world_id,
            split_id=split.split_id,
            bias_claim_ids=split.cluster_b,
            confidence=split.centroid_distance,
        )

        created += 2

        logger.info(
            "Promoted split %s → worlds %s / %s (centroid_distance=%.3f)",
            split.split_id,
            world_a,
            world_b,
            split.centroid_distance,
        )

    return created


# ============================================================
# World creation helper
# ============================================================

async def _create_world(
    db: Database,
    *,
    world_key: str,
    parent_world_id: UUID | None,
    split_id: UUID,
    bias_claim_ids: List[str],
    confidence: float,
) -> UUID:
    """
    Insert a tentative world row using the real schema.

    Uses:
      - world_key (unique)
      - parent_world_id
      - world_type = 'tentative'
      - meta JSONB for provenance
    """

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.world (
            world_key,
            world_type,
            parent_world_id,
            meta
        )
        VALUES (
            $1,
            'tentative',
            $2,
            jsonb_build_object(
                'seed_split_id', $3,
                'bias_claim_ids', $4,
                'confidence', $5,
                'source', 'world_split_candidate'
            )
        )
        ON CONFLICT (world_key) DO NOTHING
        RETURNING world_id
        """,
        world_key,
        parent_world_id,
        str(split_id),
        bias_claim_ids,
        confidence,
    )

    if row:
        return row["world_id"]

    # Conflict case: fetch existing world_id
    existing = await db.fetchrow(
        """
        SELECT world_id
        FROM aios.world
        WHERE world_key = $1
        """,
        world_key,
    )

    return existing["world_id"]
