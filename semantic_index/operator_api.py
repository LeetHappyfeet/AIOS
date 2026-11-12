"""Read-only operator discovery endpoints for semantic topology inspection.

These endpoints expose SQL-derived diagnostics, never trigger reclustering or
change epistemic decisions. Vector geometry remains advisory.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from aios_app.semantic_index.config import SemanticIndexConfig


def topology_router(db):
    router = APIRouter(prefix="/semantic/topology", tags=["semantic-topology"])

    @router.get("/status")
    async def status():
        cfg = SemanticIndexConfig()
        run = await db.fetchrow("""
            SELECT run_id, status, embedding_version, algorithm_version,
                   cluster_count, outlier_count, started_at, completed_at
            FROM aios.semantic_cluster_run
            WHERE embedding_version=$1
            ORDER BY started_at DESC LIMIT 1
        """, cfg.embedding_version)
        counts = await db.fetchrow("""
            SELECT
              (SELECT count(*) FROM aios.proposition) AS propositions,
              (SELECT count(*) FROM aios.semantic_vector_index_state
               WHERE object_type='proposition' AND qdrant_collection=$1
                 AND embedding_version=$2) AS indexed_propositions,
              (SELECT count(*) FROM aios.semantic_neighbor_candidate
               WHERE embedding_version=$2 AND status='candidate') AS neighbor_pairs,
              (SELECT count(*) FROM aios.semantic_cluster_candidate c
               JOIN aios.semantic_cluster_run r ON r.run_id=c.run_id
               WHERE c.embedding_version=$2 AND c.status='candidate'
                 AND r.status='done') AS active_clusters
        """, cfg.proposition_collection, cfg.embedding_version)
        data = dict(counts)
        current = dict(run) if run else None
        if not data["propositions"]:
            message = "No propositions have been recorded yet."
        elif not data["indexed_propositions"]:
            message = "Propositions exist but none are indexed for the active embedding version."
        elif not current:
            message = "Vectors exist, but no clustering run has been recorded for this embedding version."
        elif current["status"] != "done":
            message = "The latest clustering run is not complete; inspect its status."
        elif not data["active_clusters"]:
            message = "No qualifying active clusters. Inspect indexed propositions or outliers instead."
        else:
            message = "Active semantic clusters are available for inspection."
        return {"embedding_version": cfg.embedding_version,
                "collection": cfg.proposition_collection,
                "counts": data, "latest_run": current, "message": message}

    @router.get("/propositions")
    async def propositions(q: str = "", limit: int = 30, offset: int = 0,
                           indexed_only: bool = True):
        if not 1 <= limit <= 100 or offset < 0 or len(q) > 250:
            raise HTTPException(422, "limit must be 1..100, offset nonnegative, query at most 250 characters")
        cfg = SemanticIndexConfig()
        # Escape LIKE wildcards: a search is literal substring matching, not SQL patterns.
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = await db.fetch("""
            SELECT p.proposition_id, p.canonical_text, p.topic_key, p.polarity,
                   EXISTS (
                     SELECT 1 FROM aios.semantic_vector_index_state s
                     WHERE s.object_type='proposition'
                       AND s.object_key=p.proposition_id::text
                       AND s.qdrant_collection=$3 AND s.embedding_version=$4
                   ) AS indexed
            FROM aios.proposition p
            WHERE ($1='' OR p.canonical_text ILIKE '%' || $1 || '%' ESCAPE '\\')
              AND (NOT $2 OR EXISTS (
                SELECT 1 FROM aios.semantic_vector_index_state s
                WHERE s.object_type='proposition'
                  AND s.object_key=p.proposition_id::text
                  AND s.qdrant_collection=$3 AND s.embedding_version=$4
              ))
            ORDER BY p.created_at DESC, p.proposition_id DESC
            LIMIT $5 OFFSET $6
        """, needle, indexed_only, cfg.proposition_collection, cfg.embedding_version,
             limit, offset)
        return {"results": [dict(row) for row in rows], "limit": limit,
                "offset": offset, "query": q, "indexed_only": indexed_only}

    @router.get("/outliers")
    async def outliers(limit: int = 40, offset: int = 0):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "limit must be 1..100 and offset nonnegative")
        cfg = SemanticIndexConfig()
        rows = await db.fetch("""
            SELECT o.proposition_id, p.canonical_text, p.topic_key, o.reason,
                   o.nearest_similarity, o.nearest_proposition_id
            FROM aios.semantic_outlier_candidate o
            JOIN aios.semantic_cluster_run r ON r.run_id=o.run_id
            JOIN aios.proposition p ON p.proposition_id=o.proposition_id
            WHERE o.status='candidate' AND r.status='done'
              AND o.embedding_version=$1
            ORDER BY o.nearest_similarity DESC NULLS LAST, o.proposition_id
            LIMIT $2 OFFSET $3
        """, cfg.embedding_version, limit, offset)
        return {"outliers": [dict(row) for row in rows],
                "limit": limit, "offset": offset}

    return router
