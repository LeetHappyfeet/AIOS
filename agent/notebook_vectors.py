"""Optional project-private semantic index. PostgreSQL remains the access authority.

Only background indexing uses embeddings. Retrieval verifies each vector hit against
instance, timeline, notebook and goal in SQL before revealing any note text.
"""
from __future__ import annotations

import asyncio
import os
from uuid import UUID

from aios_app.semantic_index.config import SemanticIndexConfig
from aios_app.semantic_index.service import _get_embedder
from aios_app.semantic_index.store import QdrantStore


COLLECTION = os.getenv("AIOS_QDRANT_NOTEBOOK_COLLECTION", "project_notebooks_v1")


def _backend():
    cfg = SemanticIndexConfig()
    embedder = _get_embedder(cfg)
    store = QdrantStore(cfg.qdrant_url, cfg.qdrant_api_key, COLLECTION, embedder.dim)
    store.ensure_collection()
    return embedder, store


def _index_blocking(items):
    from qdrant_client.http import models as qm
    embedder, store = _backend()
    vectors = embedder.embed([row["content"] for row in items])
    points = []
    for row, vec in zip(items, vectors):
        points.append(qm.PointStruct(
            id=str(row["entry_id"]), vector=vec,
            payload={
                "instance_id": str(row["instance_id"]),
                "timeline_id": str(row["source_timeline_id"]),
                "goal_id": str(row["goal_id"]),
                "notebook_id": str(row["notebook_id"]),
                "object_type": "project_note",
            },
        ))
    store.upsert(points)


def _search_blocking(query, scope, limit):
    from qdrant_client.http import models as qm
    embedder, store = _backend()
    vector = embedder.embed([query])[0]
    required = [
        qm.FieldCondition(key=k, match=qm.MatchValue(value=str(v)))
        for k, v in (
            ("instance_id", scope["instance_id"]),
            ("timeline_id", scope["source_timeline_id"]),
            ("goal_id", scope["goal_id"]),
            ("notebook_id", scope["notebook_id"]),
        )
    ]
    return store.search(vector, top_k=limit, qdrant_filter=qm.Filter(must=required))


async def index_notebook(db, notebook_id: UUID, instance_id: UUID, *, batch=24):
    notebook = await db.fetchrow(
        """SELECT notebook_id FROM aios.character_notebook
           WHERE notebook_id=$1 AND instance_id=$2""", notebook_id, instance_id)
    if not notebook:
        return {"indexed": 0}
    rows = await db.fetch(
        """SELECT e.entry_id,e.content,e.instance_id,e.source_timeline_id,
                  e.goal_id,e.notebook_id
           FROM aios.character_notebook_entry e
           WHERE e.notebook_id=$1
             AND NOT EXISTS (
               SELECT 1 FROM aios.character_notebook_vector_state v
               WHERE v.entry_id=e.entry_id)
           ORDER BY e.created_at,e.entry_id LIMIT $2""", notebook_id,
        max(1, min(batch, 64)))
    if not rows:
        return {"indexed": 0}
    # Offline-only embedding. If Qdrant is offline, PostgreSQL FTS still works.
    await asyncio.to_thread(_index_blocking, [dict(r) for r in rows])
    await db.execute(
        """INSERT INTO aios.character_notebook_vector_state(entry_id,notebook_id)
           SELECT e.entry_id,e.notebook_id
           FROM aios.character_notebook_entry e
           WHERE e.notebook_id=$1 AND e.entry_id=ANY($2::uuid[])
           ON CONFLICT(entry_id) DO NOTHING""",
        notebook_id, [r["entry_id"] for r in rows],
    )
    return {"indexed": len(rows)}


async def semantic_candidates(db, *, instance_id, timeline_id, goal_id,
                              notebook_id, query, limit=12):
    scope = {"instance_id": instance_id, "source_timeline_id": timeline_id,
             "goal_id": goal_id, "notebook_id": notebook_id}
    try:
        candidates = await asyncio.wait_for(
            asyncio.to_thread(_search_blocking, query, scope, min(limit * 2, 30)),
            timeout=8.0,
        )
    except Exception:
        return []  # Semantic backend may be unavailable; lexical results remain valid.
    ids = []
    for raw_id, _, _ in candidates:
        try:
            ids.append(UUID(str(raw_id)))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    # Never return Qdrant payload content. Rehydrate under exact PostgreSQL scope.
    rows = await db.fetch(
        """SELECT entry_id,content,entry_type,author_kind,created_at
           FROM aios.character_notebook_entry
           WHERE notebook_id=$1 AND instance_id=$2 AND source_timeline_id=$3
             AND goal_id=$4 AND entry_id=ANY($5::uuid[])""",
        notebook_id, instance_id, timeline_id, goal_id, ids,
    )
    by_id = {r["entry_id"]: dict(r) for r in rows}
    return [by_id[k] for k in ids if k in by_id][:limit]
