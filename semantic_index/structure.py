from __future__ import annotations

import logging
from uuid import UUID

from aios_app.db import Database
from .config import SemanticIndexConfig
from .service import _get_store
from .relation_validator import has_structural_anchor

logger = logging.getLogger("aios.semantic_structure")


async def analyze_neighbors_once(db: Database, cfg: SemanticIndexConfig) -> int:
    """Generate advisory proposition-neighbor candidates from vector geometry."""
    rows = await db.fetch(
        """
        WITH pending AS MATERIALIZED (
            SELECT p.proposition_id,p.subject_norm,p.object_norm,p.topic_key
            FROM aios.proposition p
            JOIN aios.semantic_vector_index_state s
              ON s.object_type='proposition' AND s.object_key=p.proposition_id::text
             AND s.qdrant_collection=$2 AND s.embedding_model=$3 AND s.embedding_version=$4
            LEFT JOIN aios.semantic_structure_state ss
              ON ss.proposition_id=p.proposition_id AND ss.embedding_version=$4
            WHERE (ss.proposition_id IS NULL OR ss.analyzed_at < s.indexed_at)
              AND (ss.retry_after IS NULL OR ss.retry_after<=now())
            ORDER BY p.created_at DESC,p.proposition_id DESC LIMIT $1
        )
        SELECT pending.*, aios.semantic_proposition_topology_admitted(proposition_id) AS admitted
        FROM pending
        """,
        getattr(cfg, "neighbor_batch_size", max(1, cfg.batch_size // 2)),
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
        if not row["admitted"]:
            await db.execute("""INSERT INTO aios.semantic_structure_state
                (proposition_id,embedding_version,analyzed_at,retry_after)
                VALUES($1,$2,'-infinity',now()+interval '30 seconds')
                ON CONFLICT(proposition_id) DO UPDATE SET
                    embedding_version=EXCLUDED.embedding_version,
                    analyzed_at='-infinity',retry_after=EXCLUDED.retry_after""",
                proposition_id,cfg.embedding_version)
            continue
        try:
            try:
                vector = store.vector(str(proposition_id))
            except KeyError:
                # PostgreSQL receipts can outlive Qdrant points (for example
                # after a collection reset or an interrupted external repair).
                # Retire the stale receipt and any structure receipt so the
                # normal vector-index cycle can recreate the point. A single
                # missing advisory vector must never terminate Semantic Index.
                logger.warning(
                    "Missing Qdrant proposition vector %s; invalidating stale index state for repair",
                    proposition_id,
                )
                await db.execute(
                    """
                    DELETE FROM aios.semantic_vector_index_state
                    WHERE object_type='proposition'
                      AND object_key=$1
                      AND qdrant_collection=$2
                      AND embedding_model=$3
                      AND embedding_version=$4
                    """,
                    str(proposition_id),
                    cfg.proposition_collection,
                    cfg.embedding_model,
                    cfg.embedding_version,
                )
                await db.execute(
                    """
                    DELETE FROM aios.semantic_structure_state
                    WHERE proposition_id=$1
                      AND embedding_version=$2
                    """,
                    proposition_id,
                    cfg.embedding_version,
                )
                continue

            hits = store.search(vector, top_k=cfg.neighbor_k + 1,
                                score_threshold=cfg.neighbor_min_score)
            candidates = {}
            for _, score, payload in hits:
                try:
                    other = UUID(str(payload.get("proposition_id")))
                except (ValueError, TypeError):
                    continue
                if other != proposition_id and score >= cfg.neighbor_min_score:
                    candidates[other] = max(float(score), candidates.get(other, 0.0))
            # One bounded SQL read, rather than one eligibility query per hit.
            peers = await db.fetch("""SELECT p.proposition_id,p.subject_norm,p.object_norm,p.topic_key,
                    aios.semantic_proposition_topology_admitted(p.proposition_id) AS admitted
                FROM aios.proposition p WHERE p.proposition_id=ANY($1::uuid[])""",
                list(candidates)) if candidates else []
            for peer in peers:
                if not peer["admitted"] or not has_structural_anchor(dict(row),dict(peer)):
                    continue
                other = peer["proposition_id"]
                a, b = sorted((str(proposition_id), str(other)))
                inserted = await db.fetchrow(
                    """
                    INSERT INTO aios.semantic_neighbor_candidate (
                        proposition_id, neighbor_proposition_id, similarity,
                        relation_hint, status, embedding_version, created_at, updated_at
                    )
                    VALUES ($1::uuid,$2::uuid,$3,'semantic_neighbor','candidate',$4,now(),now())
                    ON CONFLICT (proposition_id, neighbor_proposition_id, embedding_version)
                    DO UPDATE SET similarity=EXCLUDED.similarity,updated_at=now()
                    WHERE aios.semantic_neighbor_candidate.similarity < EXCLUDED.similarity
                    RETURNING 1
                    """,
                    a,b,candidates[other],cfg.embedding_version)
                if inserted:
                    written += 1
        except Exception:
            # Only successful neighbor analysis earns a structure receipt.
            # Unexpected failures remain visible and retryable rather than
            # being falsely marked complete.
            raise
        else:
            await db.execute(
                """
                INSERT INTO aios.semantic_structure_state (
                    proposition_id, embedding_version, analyzed_at
                )
                VALUES ($1,$2,now())
                ON CONFLICT (proposition_id)
                DO UPDATE SET embedding_version=EXCLUDED.embedding_version,
                              analyzed_at=now(),retry_after='-infinity'
                """,
                proposition_id, cfg.embedding_version,
            )

    if written:
        logger.info("Generated %d advisory semantic neighbor candidates", written)
    return written
