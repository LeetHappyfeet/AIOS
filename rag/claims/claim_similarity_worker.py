from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple
from uuid import UUID

from aios_app.db import Database
from ..rag_config import RagConfig
from .claim_query_service import ClaimQueryService, ClaimFilters

logger = logging.getLogger("aios.rag.claim_similarity")


# ============================================================
# Worker identity
# ============================================================

WORKER_VERSION = "v1-nearest-neighbor-graph"


# ============================================================
# Config defaults (safe + conservative)
# ============================================================

DEFAULT_TOP_K = 50
DEFAULT_SIMILARITY_THRESHOLD = 0.75


# ============================================================
# Fetch claims to process
# ============================================================

async def fetch_claim_ids(
    db: Database,
    *,
    limit: int,
) -> List[UUID]:
    """
    Fetch claim IDs that exist in SQL.
    We deliberately do NOT try to track per-claim state here;
    idempotency is enforced at the edge table level.
    """
    rows = await db.fetch(
        """
        SELECT claim_id
        FROM aios.claim_candidate
        ORDER BY created_at, claim_id
        LIMIT $1
        """,
        limit,
    )
    return [r["claim_id"] for r in rows]


# ============================================================
# Core worker
# ============================================================

async def build_similarity_edges_once(
    db: Database,
    cfg: RagConfig,
    *,
    batch_size: int = 200,
    top_k: int = DEFAULT_TOP_K,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    world_key: str = "liminal",
) -> int:
    """
    Build claim similarity edges for a bounded batch.

    Returns:
      number of edges inserted
    """
    qs = ClaimQueryService(
        RagConfig(
            qdrant_collection=cfg.qdrant_collection,
            embedding_model=cfg.embedding_model,
            embedding_version=cfg.embedding_version,
            embedding_device=cfg.embedding_device,
            default_top_k=top_k,
        )
    )

    filters = ClaimFilters(
        world_key=world_key,
        embedding_model=cfg.embedding_model,
        embedding_version=cfg.embedding_version,
    )

    claim_ids = await fetch_claim_ids(db, limit=batch_size)
    if not claim_ids:
        return 0

    inserted = 0

    for seed_id in claim_ids:
        neighbors = qs.search_by_claim_id(
            seed_id,
            top_k=top_k,
            filters=filters,
        )

        for cid_str, sim, _payload in neighbors:
            try:
                other_id = UUID(cid_str)
            except Exception:
                continue

            if other_id == seed_id:
                continue

            if sim < similarity_threshold:
                continue

            a_id, b_id = sorted([seed_id, other_id])

            # Insert edge (idempotent)
            res = await db.execute(
                """
                INSERT INTO aios.claim_similarity_edge (
                  claim_a_id,
                  claim_b_id,
                  similarity,
                  embedding_model,
                  embedding_version
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (claim_a_id, claim_b_id, embedding_model, embedding_version)
                DO NOTHING
                """,
                a_id,
                b_id,
                float(sim),
                cfg.embedding_model,
                cfg.embedding_version,
            )

            # asyncpg returns command tag, not rowcount; we count attempts
            inserted += 1

    logger.info(
        "Inserted ~%d claim similarity edges [%s | %s:%s]",
        inserted,
        cfg.qdrant_collection,
        cfg.embedding_model,
        cfg.embedding_version,
    )

    return inserted


# ============================================================
# Long-running loop
# ============================================================

async def run_forever(
    db: Database,
    cfg: RagConfig,
    *,
    poll_seconds: int = 5,
    batch_size: int = 200,
) -> None:
    """
    Continuously build similarity edges in small batches.
    """
    import asyncio

    while True:
        n = await build_similarity_edges_once(
            db,
            cfg,
            batch_size=batch_size,
        )
        if n == 0:
            await asyncio.sleep(poll_seconds)
