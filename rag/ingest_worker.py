from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List
from uuid import UUID

from qdrant_client.http import models as qm

from aios_app.db import Database
from .rag_config import RagConfig
from .embeddings import Embedder, stable_text_hash
from .qdrant_store import QdrantStore
from .index_state import mark_error, mark_indexed

logger = logging.getLogger("aios.rag.ingest")

# ============================================================
# Process-local singletons
# ============================================================

_EMBEDDER: Embedder | None = None
_STORE: QdrantStore | None = None


def _get_embedder(cfg: RagConfig) -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        logger.info(
            "Initializing Embedder [%s | device=%s]",
            cfg.embedding_model,
            cfg.embedding_device,
        )
        _EMBEDDER = Embedder(
            cfg.embedding_model,
            device=cfg.embedding_device,
        )
    return _EMBEDDER


def _get_store(cfg: RagConfig, *, vector_dim: int) -> QdrantStore:
    global _STORE
    if _STORE is None:
        logger.info(
            "Initializing QdrantStore [%s]",
            cfg.qdrant_collection,
        )
        _STORE = QdrantStore(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key,
            collection=cfg.qdrant_collection,
            vector_dim=vector_dim,
        )
        _STORE.ensure_collection()
    return _STORE


# ============================================================
# Data model
# ============================================================


def initialize_backend(cfg: RagConfig, *, warmup: bool = True) -> tuple[Embedder, QdrantStore]:
    """Fully initialize the process-local RAG backend before readiness is advertised."""
    embedder = _get_embedder(cfg)
    vector_dim = embedder.dim
    store = _get_store(cfg, vector_dim=vector_dim)

    if warmup:
        logger.info("Warming embedding model [%s]", cfg.embedding_model)
        vectors = embedder.embed(["AIOS RAG warmup"])
        actual_dim = len(vectors[0]) if vectors and vectors[0] else 0
        if actual_dim != vector_dim:
            raise RuntimeError(
                f"RAG embedding warmup dimension {actual_dim} does not match expected {vector_dim}"
            )

    logger.info(
        "RAG backend initialized [model=%s dim=%d collection=%s]",
        cfg.embedding_model,
        vector_dim,
        cfg.qdrant_collection,
    )
    return embedder, store

@dataclass
class SectionRow:
    section_id: UUID
    content: str
    document_id: UUID | None
    node_id: UUID | None
    section_path: str | None
    section_order: int | None
    created_at_iso: str

    # optional future enrichments
    timeline_id: str | None = None
    world_id: str | None = None
    character_id: str | None = None
    source_type: str | None = None
    source_domain: str | None = None
    event_id: int | None = None


# ============================================================
# Fetch sections needing indexing (version-aware)
# ============================================================

async def fetch_unindexed_sections(
    db: Database,
    *,
    batch_size: int,
    cfg: RagConfig,
) -> List[SectionRow]:
    rows = await db.fetch(
        """
        SELECT
            ds.section_id,
            ds.content,
            ds.document_id,
            ds.node_id,
            ds.section_path,
            ds.section_order,
            dn.created_at::text AS created_at_iso
        FROM aios.document_section ds
        JOIN aios.dag_node dn
          ON dn.node_id = ds.node_id
        WHERE ds.content IS NOT NULL
          AND length(ds.content) > 0
          AND NOT EXISTS (
              SELECT 1
              FROM aios.vector_index_state vis
              WHERE vis.section_id = ds.section_id
                AND vis.qdrant_collection = $2
                AND vis.embedding_model = $3
                AND vis.embedding_version = $4
          )
        ORDER BY dn.created_at
        LIMIT $1
        """,
        int(batch_size),
        cfg.qdrant_collection,
        cfg.embedding_model,
        cfg.embedding_version,
    )

    return [
        SectionRow(
            section_id=r["section_id"],
            content=r["content"],
            document_id=r["document_id"],
            node_id=r["node_id"],
            section_path=r["section_path"],
            section_order=r["section_order"],
            created_at_iso=r["created_at_iso"],
        )
        for r in rows
    ]


# ============================================================
# Qdrant payload builder
# ============================================================

def build_payload(
    cfg: RagConfig,
    section: SectionRow,
    *,
    vector_hash: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "section_id": str(section.section_id),
        "document_id": str(section.document_id) if section.document_id else None,
        "node_id": str(section.node_id) if section.node_id else None,
        "section_path": section.section_path,
        "section_order": section.section_order,
        "created_at": section.created_at_iso,
        "embedding_model": cfg.embedding_model,
        "embedding_version": cfg.embedding_version,
        "vector_hash": vector_hash,
        # future fields
        "timeline_id": section.timeline_id,
        "world_id": section.world_id,
        "character_id": section.character_id,
        "source_type": section.source_type,
        "source_domain": section.source_domain,
        "event_id": section.event_id,
    }

    # Strip nulls
    return {k: v for k, v in payload.items() if v is not None}


# ============================================================
# Ingestion worker (single bounded pass)
# ============================================================

async def ingest_once(db: Database, cfg: RagConfig) -> int:
    """
    Perform ONE bounded ingestion pass.

    Returns number of sections indexed.
    """

    embedder, store = initialize_backend(cfg, warmup=False)

    sections = await fetch_unindexed_sections(
        db,
        batch_size=cfg.batch_size,
        cfg=cfg,
    )

    if not sections:
        return 0

    texts = [s.content for s in sections]
    hashes = [stable_text_hash(t) for t in texts]
    vectors = embedder.embed(texts)

    points: List[qm.PointStruct] = []
    for section, vec_hash, vector in zip(sections, hashes, vectors):
        points.append(
            qm.PointStruct(
                id=str(section.section_id),
                vector=vector,
                payload=build_payload(cfg, section, vector_hash=vec_hash),
            )
        )

    try:
        store.upsert_points(points)
    except Exception as exc:
        logger.exception("Qdrant upsert failed")
        for section in sections:
            await mark_error(
                db,
                section_id=section.section_id,
                qdrant_collection=cfg.qdrant_collection,
                embedding_model=cfg.embedding_model,
                embedding_version=cfg.embedding_version,
                error=str(exc),
            )
        raise

    for section, vec_hash in zip(sections, hashes):
        await mark_indexed(
            db,
            section_id=section.section_id,
            qdrant_collection=cfg.qdrant_collection,
            embedding_model=cfg.embedding_model,
            embedding_version=cfg.embedding_version,
            vector_hash=vec_hash,
        )

    logger.info(
        "Indexed %d document_section rows into Qdrant [%s | %s:%s]",
        len(sections),
        cfg.qdrant_collection,
        cfg.embedding_model,
        cfg.embedding_version,
    )

    return len(sections)
