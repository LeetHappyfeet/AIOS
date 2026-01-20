# aios_app/rag/claims/claim_query_service.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from qdrant_client.http import models as qm

from ..rag_config import RagConfig
from ..embeddings import Embedder
from ..qdrant_store import QdrantStore


# ============================================================
# Process-local singletons (query side)
# ============================================================

_QUERY_EMBEDDER: Embedder | None = None
_QUERY_STORE: QdrantStore | None = None


def _get_embedder(cfg: RagConfig) -> Embedder:
    global _QUERY_EMBEDDER
    if _QUERY_EMBEDDER is None:
        _QUERY_EMBEDDER = Embedder(
            cfg.embedding_model,
            device=cfg.embedding_device,
        )
    return _QUERY_EMBEDDER


def _get_store(cfg: RagConfig, *, vector_dim: int) -> QdrantStore:
    global _QUERY_STORE
    if _QUERY_STORE is None:
        _QUERY_STORE = QdrantStore(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key,
            collection=cfg.qdrant_collection,
            vector_dim=vector_dim,
        )
        _QUERY_STORE.ensure_collection()
    return _QUERY_STORE


# ============================================================
# Claim-specific filters
# ============================================================

@dataclass
class ClaimFilters:
    """
    Claim-level filters.

    These are metadata-only filters.
    They do NOT imply truth or world mutation.
    """
    world_key: Optional[str] = None        # e.g. "liminal"
    status: Optional[str] = None           # e.g. "pending"
    extraction_ver: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_version: Optional[str] = None
    created_at_gte: Optional[str] = None
    created_at_lte: Optional[str] = None


def to_qdrant_filter(f: ClaimFilters) -> Optional[qm.Filter]:
    must: List[qm.FieldCondition] = []

    def kw(key: str, val: Optional[str]) -> None:
        if val is not None:
            must.append(
                qm.FieldCondition(
                    key=key,
                    match=qm.MatchValue(value=val),
                )
            )

    kw("world_key", f.world_key)
    kw("status", f.status)
    kw("extraction_ver", f.extraction_ver)
    kw("embedding_model", f.embedding_model)
    kw("embedding_version", f.embedding_version)

    if f.created_at_gte or f.created_at_lte:
        must.append(
            qm.FieldCondition(
                key="created_at",
                range=qm.DatetimeRange(
                    gte=f.created_at_gte,
                    lte=f.created_at_lte,
                ),
            )
        )

    if not must:
        return None

    return qm.Filter(must=must)


# ============================================================
# Claim query service
# ============================================================

class ClaimQueryService:
    """
    Thin, claim-only query layer over Qdrant.

    Properties:
    - operates ONLY on the claims collection
    - does NOT inject results into prompts
    - returns structural similarity signals only
    """

    def __init__(self, cfg: RagConfig):
        self.cfg = cfg
        self.embedder = _get_embedder(cfg)
        self.store = _get_store(cfg, vector_dim=self.embedder.dim)

    # --------------------------------------------------------
    # Text → claim similarity
    # --------------------------------------------------------

    def search_by_text(
        self,
        text: str,
        *,
        top_k: Optional[int] = None,
        filters: Optional[ClaimFilters] = None,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """
        Embed free text and find similar claims.

        Returns:
          [(claim_id, similarity_score, payload), ...]
        """
        vector = self.embedder.embed([text])[0]
        qf = to_qdrant_filter(filters or ClaimFilters())

        results = self.store.search(
            vector=vector,
            limit=top_k or self.cfg.default_top_k,
            filter=qf,
        )

        return [
            (str(p.id), float(p.score), dict(p.payload or {}))
            for p in results
        ]

    # --------------------------------------------------------
    # Claim → claim similarity
    # --------------------------------------------------------

    def search_by_claim_id(
        self,
        claim_id: UUID | str,
        *,
        top_k: Optional[int] = None,
        filters: Optional[ClaimFilters] = None,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """
        Use an existing claim vector as the query seed.
        """
        # pull the stored vector
        pts = self.store.client.retrieve(
            collection_name=self.store.collection,
            ids=[str(claim_id)],
            with_vectors=True,
            with_payload=False,
        )

        if not pts:
            raise KeyError(f"Claim not found in Qdrant: {claim_id}")

        vec = pts[0].vector
        if isinstance(vec, dict):
            vec = next(iter(vec.values()))

        qf = to_qdrant_filter(filters or ClaimFilters())

        results = self.store.search(
            vector=list(vec),
            limit=top_k or self.cfg.default_top_k,
            filter=qf,
        )

        return [
            (str(p.id), float(p.score), dict(p.payload or {}))
            for p in results
        ]
