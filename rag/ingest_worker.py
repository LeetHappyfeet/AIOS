# aios_app/rag/ingest_worker.py

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

from qdrant_client.http import models as qm

from aios_app.db import Database
from .rag_config import RagConfig
from .embeddings import Embedder, stable_text_hash
from .qdrant_store import QdrantStore
from .index_state import get_index_state, mark_error, mark_indexed

logger = logging.getLogger("aios.rag.ingest")


@dataclass
class SectionRow:
    section_id: UUID
    content: str
    document_id: UUID | None
    node_id: UUID | None
    section_path: str | None
    section_order: int | None
    created_at_iso: str | None

    # payload extras (optional, safe to be None)
    timeline_id: str | None = None
    world_id: str | None = None
    character_id: str | None = None
    source_type: str | None = None
    source_domain: str | None = None
    event_id: int | None = None


async def fetch_unindexed_sections(
    db: Database,
    *,
    batch_size: int,
) -> List[SectionRow]:
    # Keep this SELECT conservative. Extend later once your schema fields are confirmed.
    rows = await db.fetch(
        f"""
        SELECT
          ds.section_id,
          ds.content,
          ds.document_id,
          ds.node_id,
          ds.section_path,
          ds.section_order,
          -- created_at might not exist; if it doesn't, set NULL in your schema or add it later.
          NULL::text AS created_at_iso
        FROM aios.document_section ds
        WHERE ds.content IS NOT NULL
          AND length(ds.content) > 0
          AND NOT EXISTS (
            SELECT 1
            FROM aios.vector_index_state vis
            WHERE vis.section_id = ds.section_id
          )
        ORDER BY ds.section_id
        LIMIT {int(batch_size)}
        """
    )

    out: List[SectionRow] = []
    for r in rows:
        out.append(
            SectionRow(
                section_id=r["section_id"],
                content=r["content"],
                document_id=r["document_id"],
                node_id=r["node_id"],
                section_path=r["section_path"],
                section_order=r["section_order"],
                created_at_iso=r["created_at_iso"],
            )
        )
    return out


def build_payload(cfg: RagConfig, s: SectionRow, *, vector_hash: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "section_id": str(s.section_id),
        "qdrant_collection": cfg.qdrant_collection,
        "embedding_model": cfg.embedding_model,
        "embedding_version": cfg.embedding_version,
        "vector_hash": vector_hash,

        # from document_section
        "document_id": str(s.document_id) if s.document_id else None,
        "node_id": str(s.node_id) if s.node_id else None,
        "section_path": s.section_path,
        "section_order": s.section_order,
        "created_at": s.created_at_iso,

        # optional extras (will be None for now)
        "timeline_id": s.timeline_id,
        "world_id": s.world_id,
        "character_id": s.character_id,
        "source_type": s.source_type,
        "source_domain": s.source_domain,
        "event_id": s.event_id,
    }
    # Remove None fields (keeps payload clean)
    return {k: v for k, v in payload.items() if v is not None}


async def ingest_once(db: Database, cfg: RagConfig) -> int:
    embedder = Embedder(cfg.embedding_model, device=cfg.embedding_device)
    store = QdrantStore(
        url=cfg.qdrant_url,
        api_key=cfg.qdrant_api_key,
        collection=cfg.qdrant_collection,
        vector_dim=embedder.dim,
    )
    store.ensure_collection()

    sections = await fetch_unindexed_sections(db, batch_size=cfg.batch_size)
    if not sections:
        return 0

    texts = [s.content for s in sections]
    hashes = [stable_text_hash(t) for t in texts]
    vecs = embedder.embed(texts)

    points: List[qm.PointStruct] = []
    for s, h, v in zip(sections, hashes, vecs):
        payload = build_payload(cfg, s, vector_hash=h)
        points.append(
            qm.PointStruct(
                id=str(s.section_id),  # use UUID string as point id (best: 1:1 with section_id)
                vector=v,
                payload=payload,
            )
        )

    try:
        store.upsert_points(points)
    except Exception as e:
        # record per-section errors to avoid infinite loops
        for s in sections:
            await mark_error(
                db,
                section_id=s.section_id,
                qdrant_collection=cfg.qdrant_collection,
                embedding_model=cfg.embedding_model,
                embedding_version=cfg.embedding_version,
                error=str(e),
            )
        raise

    # Mark indexed
    for s, h in zip(sections, hashes):
        await mark_indexed(
            db,
            section_id=s.section_id,
            qdrant_collection=cfg.qdrant_collection,
            embedding_model=cfg.embedding_model,
            embedding_version=cfg.embedding_version,
            vector_hash=h,
        )

    logger.info("Indexed %d document_section rows into Qdrant", len(sections))
    return len(sections)
