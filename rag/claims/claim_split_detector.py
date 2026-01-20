# aios_app/rag/claims/claim_split_detector.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
from uuid import UUID, uuid4

import numpy as np

from aios_app.db import Database
from ..rag_config import RagConfig
from .claim_query_service import ClaimQueryService, ClaimFilters

logger = logging.getLogger("aios.rag.claim_split_detector")


# ============================================================
# Result model (candidate-only)
# ============================================================

@dataclass
class ClaimSplitResult:
    split_id: UUID
    seed_claim_id: UUID
    cluster_count: int
    centroid_distance: float
    boundary_pairs: List[Dict[str, Any]]
    cluster_a: Dict[str, Any]
    cluster_b: Dict[str, Any]


# ============================================================
# Math helpers
# ============================================================

def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    # embeddings are normalized → cosine similarity = dot
    return float(1.0 - float(np.dot(a, b)))


# ============================================================
# Split detection (seed + local growth)
# ============================================================

async def detect_claim_split(
    db: Database,
    cfg: RagConfig,
    *,
    seed_claim_id: UUID,
    world_key: str = "liminal",
    top_k: int = 40,
    min_cluster_size: int = 4,
) -> ClaimSplitResult | None:
    """
    Detect a possible epistemic split among claims near a seed claim.

    Algorithm:
      1. Pull k-nearest neighboring claims
      2. Filter by similarity threshold
      3. Perform simple k=2 clustering
      4. Measure centroid divergence
      5. Emit boundary claim pairs

    Output is ADVISORY ONLY.
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

    # --------------------------------------------------------
    # 1. Neighborhood expansion from seed
    # --------------------------------------------------------

    neighbors = qs.search_by_claim_id(
        seed_claim_id,
        top_k=top_k,
        filters=filters,
    )

    node_ids: List[str] = [str(seed_claim_id)]
    for cid, score, _payload in neighbors:
        if score >= cfg.edge_threshold and cid not in node_ids:
            node_ids.append(cid)

    if len(node_ids) < min_cluster_size * 2:
        logger.debug(
            "Claim split aborted: insufficient neighborhood (%d nodes)",
            len(node_ids),
        )
        return None

    # --------------------------------------------------------
    # 2. Pull vectors
    # --------------------------------------------------------

    vecs: List[np.ndarray] = []
    for cid in node_ids:
        pts = qs.store.client.retrieve(
            collection_name=qs.store.collection,
            ids=[cid],
            with_vectors=True,
            with_payload=False,
        )
        if not pts:
            continue
        v = pts[0].vector
        if isinstance(v, dict):
            v = next(iter(v.values()))
        vecs.append(np.asarray(v, dtype=np.float32))

    if len(vecs) < min_cluster_size * 2:
        return None

    X = np.stack(vecs, axis=0)

    # --------------------------------------------------------
    # 3. Simple 2-means clustering
    # --------------------------------------------------------

    rng = np.random.default_rng(42)
    c1 = X[rng.integers(0, len(X))]
    c2 = X[rng.integers(0, len(X))]

    for _ in range(12):
        d1 = 1.0 - (X @ c1)
        d2 = 1.0 - (X @ c2)
        labels = (d2 < d1).astype(np.int32)

        if labels.sum() == 0 or labels.sum() == len(labels):
            break

        c1 = X[labels == 0].mean(axis=0)
        c2 = X[labels == 1].mean(axis=0)
        c1 /= np.linalg.norm(c1)
        c2 /= np.linalg.norm(c2)

    A_idx = np.where(labels == 0)[0].tolist()
    B_idx = np.where(labels == 1)[0].tolist()

    if len(A_idx) < min_cluster_size or len(B_idx) < min_cluster_size:
        return None

    # --------------------------------------------------------
    # 4. Centroid divergence
    # --------------------------------------------------------

    centroid_a = X[A_idx].mean(axis=0)
    centroid_a /= np.linalg.norm(centroid_a)

    centroid_b = X[B_idx].mean(axis=0)
    centroid_b /= np.linalg.norm(centroid_b)

    dist = cosine_distance(centroid_a, centroid_b)

    # --------------------------------------------------------
    # 5. Boundary pairs (closest cross-cluster neighbors)
    # --------------------------------------------------------

    boundary_pairs: List[Dict[str, Any]] = []

    for i in A_idx:
        sims = X[B_idx] @ X[i]
        j_local = int(np.argmax(sims))
        j = B_idx[j_local]
        boundary_pairs.append(
            {
                "a": node_ids[i],
                "b": node_ids[j],
                "similarity": float(sims[j_local]),
            }
        )

    split_id = uuid4()

    cluster_a = {"size": len(A_idx)}
    cluster_b = {"size": len(B_idx)}

    # --------------------------------------------------------
    # 6. Persist as CANDIDATE ONLY
    # --------------------------------------------------------

    await db.execute(
        """
        INSERT INTO aios.world_split_candidate (
          split_id,
          seed_section_id,
          cluster_count,
          cluster_a,
          cluster_b,
          centroid_distance,
          boundary_pairs
        )
        VALUES ($1, $2, 2, $3::jsonb, $4::jsonb, $5, $6::jsonb)
        """,
        split_id,
        seed_claim_id,
        cluster_a,
        cluster_b,
        float(dist),
        boundary_pairs,
    )

    return ClaimSplitResult(
        split_id=split_id,
        seed_claim_id=seed_claim_id,
        cluster_count=2,
        centroid_distance=float(dist),
        boundary_pairs=boundary_pairs,
        cluster_a=cluster_a,
        cluster_b=cluster_b,
    )
