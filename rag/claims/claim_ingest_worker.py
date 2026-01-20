# aios_app/rag/claims/claim_ingest_worker.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List
from uuid import UUID

from qdrant_client.http import models as qm

from aios_app.db import Database
from ..rag_config import RagConfig
from ..embeddings import Embedder, stable_text_hash
from ..qdrant_store import QdrantStore
from ..index_state import mark_error, mark_indexed

logger = logging.getLogger("aios.rag.claim_ingest")

# ============================================================
# Process-local singletons
# ============================================================

_EMBEDDER: Embedder | None = None
_STORE: QdrantStore | None = None


def _get_embedder(cfg: RagConfig) -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        logger.info(
            "Initializing Claim Embedder [%s | device=%s]",
            cfg.embedding_model,
            cfg.embedding_device,
        )
        _EMBEDDER = Embedder(cfg.embedding_model, device=cfg.embedding_device)
    return _EMBEDDER


def _get_store(cfg: RagConfig, *, vector_dim: int) -> QdrantStore:
    global _STORE
    if _STORE is None:
        logger.info(
            "Initializing Claim QdrantStore [%s]",
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

@dataclass
class ClaimRow:
    claim_id: UUID
    sentence_id: UUID
    document_id: UUID | None
    subject: str | None
    predicate: str | None
    object: str | None
    raw_text: str
    extraction_version: str
    created_at_iso: str

    # index-state (may be null)
    existing_hash: str | None = None

    # enforced epistemic fields
    world_key: str = "liminal"
    status: str = "pending"


# ============================================================
# Embedding text selection
# ============================================================

def claim_embedding_text(c: ClaimRow) -> str:
    """
    Preferred embedding form:
      subject predicate object

    Fallback:
      raw_text
    """
    if c.subject and c.predicate and c.object:
        return f"{c.subject} {c.predicate} {c.object}"
    return c.raw_text


# ============================================================
# Fetch claims + existing index state (hash-aware)
# ============================================================

async def fetch_claims_with_state(
    db: Database,
    *,
    limit: int,
    cfg: RagConfig,
) -> List[ClaimRow]:
    rows = await db.fetch(
        """
        SELECT
            cc.claim_id,
            cc.sentence_id,
            ds.document_id,
            cc.subject,
            cc.predicate,
            cc.object,
            cc.raw_text,
            cc.extraction_ver,
            cc.created_at::text AS created_at_iso,
            vis.vector_hash AS existing_hash
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es
          ON es.sentence_id = cc.sentence_id
        LEFT JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        LEFT JOIN aios.vector_index_state vis
          ON vis.section_id = cc.claim_id
         AND vis.qdrant_collection = $2
         AND vis.embedding_model = $3
         AND vis.embedding_version = $4
        WHERE cc.raw_text IS NOT NULL
          AND length(cc.raw_text) > 0
        ORDER BY cc.created_at, cc.claim_id
        LIMIT $1
        """,
        int(limit),
        cfg.qdrant_collection,
        cfg.embedding_model,
        cfg.embedding_version,
    )

    return [
        ClaimRow(
            claim_id=r["claim_id"],
            sentence_id=r["sentence_id"],
            document_id=r["document_id"],
            subject=r["subject"],
            predicate=r["predicate"],
            object=r["object"],
            raw_text=r["raw_text"],
            extraction_version=r["extraction_ver"],
            created_at_iso=r["created_at_iso"],
            existing_hash=r["existing_hash"],
        )
        for r in rows
    ]


# ============================================================
# Qdrant payload builder
# ============================================================

def build_payload(
    cfg: RagConfig,
    claim: ClaimRow,
    *,
    vector_hash: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "claim_id": str(claim.claim_id),
        "sentence_id": str(claim.sentence_id),
        "document_id": str(claim.document_id) if claim.document_id else None,
        "world_key": claim.world_key,
        "status": claim.status,
        "extraction_ver": claim.extraction_version,
        "created_at": claim.created_at_iso,
        "embedding_model": cfg.embedding_model,
        "embedding_version": cfg.embedding_version,
        "vector_hash": vector_hash,
    }

    return {k: v for k, v in payload.items() if v is not None}


# ============================================================
# Ingestion worker (single bounded pass)
# ============================================================

async def ingest_once(db: Database, cfg: RagConfig) -> int:
    """
    Perform ONE bounded claim ingestion pass.

    Returns number of claims indexed.
    """

    embedder = _get_embedder(cfg)
    store = _get_store(cfg, vector_dim=embedder.dim)

    # Oversample so we can skip already-indexed claims
    oversample = max(cfg.batch_size * 5, cfg.batch_size)

    candidates = await fetch_claims_with_state(
        db,
        limit=oversample,
        cfg=cfg,
    )

    if not candidates:
        return 0

    to_index: List[ClaimRow] = []
    texts: List[str] = []
    hashes: List[str] = []

    for c in candidates:
        text = claim_embedding_text(c).strip()
        if not text:
            continue

        new_hash = stable_text_hash(text)

        should_index = (
            c.existing_hash is None
            or (
                cfg.reindex_on_hash_change
                and c.existing_hash != new_hash
            )
        )

        if not should_index:
            continue

        to_index.append(c)
        texts.append(text)
        hashes.append(new_hash)

        if len(to_index) >= cfg.batch_size:
            break

    if not to_index:
        return 0

    vectors = embedder.embed(texts)

    points: List[qm.PointStruct] = []
    for claim, vec_hash, vector in zip(to_index, hashes, vectors):
        points.append(
            qm.PointStruct(
                id=str(claim.claim_id),
                vector=vector,
                payload=build_payload(cfg, claim, vector_hash=vec_hash),
            )
        )

    try:
        store.upsert_points(points)
    except Exception as exc:
        logger.exception("Claim Qdrant upsert failed")
        for claim in to_index:
            await mark_error(
                db,
                section_id=claim.claim_id,
                qdrant_collection=cfg.qdrant_collection,
                embedding_model=cfg.embedding_model,
                embedding_version=cfg.embedding_version,
                error=str(exc),
            )
        raise

    for claim, vec_hash in zip(to_index, hashes):
        await mark_indexed(
            db,
            section_id=claim.claim_id,
            qdrant_collection=cfg.qdrant_collection,
            embedding_model=cfg.embedding_model,
            embedding_version=cfg.embedding_version,
            vector_hash=vec_hash,
        )

    logger.info(
        "Indexed %d claims into Qdrant [%s | %s:%s]",
        len(to_index),
        cfg.qdrant_collection,
        cfg.embedding_model,
        cfg.embedding_version,
    )

    return len(to_index)
