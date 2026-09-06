from __future__ import annotations

import logging

from aios_app.db import Database
from .config import SemanticIndexConfig
from .service import _get_store

logger = logging.getLogger("aios.semantic_structure")


async def analyze_neighbors_once(db: Database, cfg: SemanticIndexConfig) -> int:
    """Generate advisory proposition-neighbor candidates from vector geometry."""
    rows = await db.fetch(
        """
        SELECT p.proposition_id
        FROM aios.proposition p
        JOIN aios.semantic_vector_index_state s
          ON s.object_type='proposition'
         AND s.object_key=p.proposition_id::text
         AND s.qdrant_collection=$2
         AND s.embedding_model=$3
         AND s.embedding_version=$4
        LEFT JOIN aios.semantic_structure_state ss
          ON ss.proposition_id=p.proposition_id
         AND ss.embedding_version=$4
        WHERE ss.proposition_id IS NULL
        ORDER BY p.created_at
        LIMIT $1
        """,
        max(1, cfg.batch_size // 2),
        cfg.proposition_collection,
        cfg.embedding_model,
        cfg.embedding_version,
    )
    if not rows:
        return 0

    store = _get_store(cfg, cfg.proposition_collection)
    written = 0
    for row in rows:
        proposition_id = row["proposition_id"]
        try:
            vector = store.vector(str(proposition_id))
            hits = store.search(vector, top_k=cfg.neighbor_k + 1)
            for _, score, payload in hits:
                other = payload.get("proposition_id")
                if not other or other == str(proposition_id) or score < cfg.neighbor_min_score:
                    continue
                a, b = sorted((str(proposition_id), str(other)))
                await db.execute(
                    """
                    INSERT INTO aios.semantic_neighbor_candidate (
                        proposition_id, neighbor_proposition_id, similarity,
                        relation_hint, status, embedding_version,
                        created_at, updated_at
                    )
                    VALUES ($1::uuid,$2::uuid,$3,'semantic_neighbor','candidate',$4,now(),now())
                    ON CONFLICT (proposition_id, neighbor_proposition_id, embedding_version)
                    DO UPDATE SET similarity=GREATEST(
                        aios.semantic_neighbor_candidate.similarity,
                        EXCLUDED.similarity
                    ), updated_at=now()
                    """,
                    a, b, float(score), cfg.embedding_version,
                )
                written += 1
        finally:
            await db.execute(
                """
                INSERT INTO aios.semantic_structure_state (
                    proposition_id, embedding_version, analyzed_at
                )
                VALUES ($1,$2,now())
                ON CONFLICT (proposition_id)
                DO UPDATE SET embedding_version=EXCLUDED.embedding_version,
                              analyzed_at=now()
                """,
                proposition_id, cfg.embedding_version,
            )

    if written:
        logger.info("Generated %d advisory semantic neighbor candidates", written)
    return written
