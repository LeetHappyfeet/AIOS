# aios_app/rag/split_detector.py

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
from uuid import UUID, uuid4

import numpy as np

from aios_app.db import Database
from .rag_config import RagConfig
from .query_api import RagQueryService, RagFilters

logger = logging.getLogger("aios.rag.split_detector")


@dataclass
class SplitResult:
    split_id: UUID
    seed_section_id: UUID
    cluster_count: int
    centroid_distance: float
    boundary_pairs: List[Dict[str, Any]]
    cluster_a: Dict[str, Any]
    cluster_b: Dict[str, Any]


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    # embeddings are normalized, so cosine similarity = dot
    sim = float(np.dot(a, b))
    return float(1.0 - sim)


async def detect_split_seed_and_grow(
    db: Database,
    cfg: RagConfig,
    *,
    seed_section_id: UUID,
    window_start_iso: str | None = None,
    window_end_iso: str | None = None,
    source_type: str = "news",
    top_k: int = 30,
) -> SplitResult | None:
    """
    Minimal seed-and-grow:
      - expand neighbors from seed (1-hop only for now)
      - keep edges above cfg.edge_threshold
      - do a crude 2-cluster split on embeddings of the gathered nodes
    """
    qs = RagQueryService(cfg)

    filters = RagFilters(
        source_type=source_type,
        created_at_gte=window_start_iso,
        created_at_lte=window_end_iso,
    )

    # 1-hop neighborhood from seed
    neighbors = qs.search_by_section(
        str(seed_section_id),
        top_k=top_k,
        filters=filters,
    )

    # Include the seed itself if present elsewhere
    node_ids: List[str] = [str(seed_section_id)]
    for sid, score, _payload in neighbors:
        if score >= cfg.edge_threshold and sid not in node_ids:
            node_ids.append(sid)

    if len(node_ids) < 8:
        return None

    # Pull vectors for all nodes
    vecs = []
    payloads = []
    for sid in node_ids:
        v = np.array(qs.store.get_vector(sid), dtype=np.float32)
        vecs.append(v)
        # payload is optional here; only used for summaries later
        payloads.append({})

    X = np.stack(vecs, axis=0)

    # Very simple 2-means (k=2) using random init
    rng = np.random.default_rng(42)
    c1 = X[rng.integers(0, len(X))]
    c2 = X[rng.integers(0, len(X))]

    for _ in range(10):
        d1 = (1.0 - (X @ c1))
        d2 = (1.0 - (X @ c2))
        labels = (d2 < d1).astype(np.int32)

        if labels.sum() == 0 or labels.sum() == len(labels):
            break

        c1 = X[labels == 0].mean(axis=0)
        c2 = X[labels == 1].mean(axis=0)
        # renormalize
        c1 = c1 / np.linalg.norm(c1)
        c2 = c2 / np.linalg.norm(c2)

    if labels.sum() == 0 or labels.sum() == len(labels):
        return None

    A_idx = np.where(labels == 0)[0].tolist()
    B_idx = np.where(labels == 1)[0].tolist()

    if len(A_idx) < 4 or len(B_idx) < 4:
        return None

    centroid_a = X[A_idx].mean(axis=0)
    centroid_a = centroid_a / np.linalg.norm(centroid_a)
    centroid_b = X[B_idx].mean(axis=0)
    centroid_b = centroid_b / np.linalg.norm(centroid_b)

    dist = cosine_distance(centroid_a, centroid_b)

    # Boundary pairs: closest cross-cluster neighbors
    boundary_pairs: List[Dict[str, Any]] = []
    for i in A_idx:
        sims = X[B_idx] @ X[i]
        j_local = int(np.argmax(sims))
        j = B_idx[j_local]
        boundary_pairs.append(
            {"a": node_ids[i], "b": node_ids[j], "similarity": float(sims[j_local])}
        )

    split_id = uuid4()

    cluster_a = {"size": len(A_idx)}
    cluster_b = {"size": len(B_idx)}

    # Write SQL candidates
    await db.execute(
        """
        INSERT INTO aios.world_split_candidate (
          split_id, seed_section_id, window_start, window_end,
          cluster_count, cluster_a, cluster_b, centroid_distance, boundary_pairs
        )
        VALUES ($1, $2, COALESCE($3::timestamptz, now() - interval '48 hours'),
                    COALESCE($4::timestamptz, now()),
                2, $5::jsonb, $6::jsonb, $7, $8::jsonb)
        """,
        split_id,
        seed_section_id,
        window_start_iso,
        window_end_iso,
        cluster_a,
        cluster_b,
        float(dist),
        boundary_pairs,
    )

    # section_cluster_assignment
    for i in A_idx:
        await db.execute(
            """
            INSERT INTO aios.section_cluster_assignment (split_id, section_id, cluster_label, score_to_centroid)
            VALUES ($1, $2, 'A', NULL)
            ON CONFLICT (split_id, section_id) DO NOTHING
            """,
            split_id,
            UUID(node_ids[i]),
        )
    for i in B_idx:
        await db.execute(
            """
            INSERT INTO aios.section_cluster_assignment (split_id, section_id, cluster_label, score_to_centroid)
            VALUES ($1, $2, 'B', NULL)
            ON CONFLICT (split_id, section_id) DO NOTHING
            """,
            split_id,
            UUID(node_ids[i]),
        )

    return SplitResult(
        split_id=split_id,
        seed_section_id=seed_section_id,
        cluster_count=2,
        centroid_distance=float(dist),
        boundary_pairs=boundary_pairs,
        cluster_a=cluster_a,
        cluster_b=cluster_b,
    )
