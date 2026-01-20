#aios_app/rag/world/section_claim_cluster_worker.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple
from uuid import UUID
from datetime import datetime

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_distances

from aios_app.db import Database
from aios_app.rag.rag_config import RagConfig
from aios_app.rag.embeddings import Embedder

logger = logging.getLogger("aios.world.section_split")


# ============================================================
# Tunables (conservative defaults)
# ============================================================

MIN_CLAIMS_PER_SECTION = 3
MIN_CENTROID_DISTANCE = 0.08   # cosine distance
MAX_SECTIONS_PER_RUN = 25


# ============================================================
# Helpers
# ============================================================

@dataclass
class ClaimVec:
    claim_id: UUID
    vector: np.ndarray


def _cluster_two(vectors: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
    """
    Cluster vectors into 2 groups and return:
      labels, centroids, centroid_distance
    """
    kmeans = KMeans(n_clusters=2, n_init=10, random_state=42)
    labels = kmeans.fit_predict(vectors)
    centroids = kmeans.cluster_centers_
    dist = cosine_distances([centroids[0]], [centroids[1]])[0][0]
    return labels, centroids, float(dist)


# ============================================================
# Main worker
# ============================================================

async def seed_section_splits_once(
    db: Database,
    cfg: RagConfig,
) -> int:
    """
    Detect claim divergence *within document sections* and
    insert rows into aios.world_split_candidate.

    Returns number of split candidates inserted.
    """

    # --------------------------------------------------------
    # 1. Find candidate sections (enough claims, not yet split)
    # --------------------------------------------------------

    sections = await db.fetch(
        """
        SELECT
            ds.section_id,
            ds.created_at AS window_start,
            ds.created_at AS window_end,
            COUNT(cc.claim_id) AS claim_count
        FROM aios.document_section ds
        JOIN aios.extracted_sentence es
          ON es.section_id = ds.section_id
        JOIN aios.claim_candidate cc
          ON cc.sentence_id = es.sentence_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.world_split_candidate wsc
            WHERE wsc.seed_section_id = ds.section_id
        )
        GROUP BY ds.section_id
        HAVING COUNT(cc.claim_id) >= $1
        ORDER BY ds.created_at
        LIMIT $2
        """,
        MIN_CLAIMS_PER_SECTION,
        MAX_SECTIONS_PER_RUN,
    )

    if not sections:
        return 0

    embedder = Embedder(cfg.embedding_model, device=cfg.embedding_device)
    inserted = 0

    # --------------------------------------------------------
    # 2. Process each section independently
    # --------------------------------------------------------

    for sec in sections:
        section_id: UUID = sec["section_id"]

        rows = await db.fetch(
            """
            SELECT
                cc.claim_id,
                COALESCE(
                  cc.subject || ' ' || cc.predicate || ' ' || cc.object,
                  cc.raw_text
                ) AS text
            FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es
              ON es.sentence_id = cc.sentence_id
            WHERE es.section_id = $1
            """,
            section_id,
        )

        if len(rows) < MIN_CLAIMS_PER_SECTION:
            continue

        texts = [r["text"] for r in rows]
        claim_ids = [r["claim_id"] for r in rows]

        vectors = embedder.embed(texts)
        X = np.array(vectors)

        # ----------------------------------------------------
        # 3. Attempt 2-cluster split
        # ----------------------------------------------------

        try:
            labels, centroids, centroid_dist = _cluster_two(X)
        except Exception:
            continue

        if centroid_dist < MIN_CENTROID_DISTANCE:
            continue

        cluster_a = []
        cluster_b = []
        boundary_pairs = []

        for cid, lbl in zip(claim_ids, labels):
            (cluster_a if lbl == 0 else cluster_b).append(str(cid))

        # Identify boundary pairs (closest cross-cluster points)
        for i, li in enumerate(labels):
            for j, lj in enumerate(labels):
                if li != lj:
                    d = cosine_distances([X[i]], [X[j]])[0][0]
                    if d < centroid_dist:
                        boundary_pairs.append({
                            "a": str(claim_ids[i]),
                            "b": str(claim_ids[j]),
                            "distance": float(d),
                        })

        # ----------------------------------------------------
        # 4. Insert split candidate
        # ----------------------------------------------------

        await db.execute(
            """
            INSERT INTO aios.world_split_candidate (
              split_id,
              seed_section_id,
              window_start,
              window_end,
              cluster_count,
              cluster_a,
              cluster_b,
              centroid_distance,
              boundary_pairs
            )
            VALUES (
              gen_random_uuid(),
              $1,
              $2,
              $3,
              2,
              $4::jsonb,
              $5::jsonb,
              $6,
              $7::jsonb
            )
            """,
            section_id,
            sec["window_start"],
            sec["window_end"],
            cluster_a,
            cluster_b,
            centroid_dist,
            boundary_pairs,
        )

        inserted += 1
        logger.info(
            "Section %s split detected (centroid_dist=%.3f, |A|=%d, |B|=%d)",
            section_id,
            centroid_dist,
            len(cluster_a),
            len(cluster_b),
        )

    return inserted
