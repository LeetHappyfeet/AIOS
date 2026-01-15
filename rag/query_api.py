# aios_app/rag/query_api.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from qdrant_client.http import models as qm

from .rag_config import RagConfig
from .embeddings import Embedder
from .qdrant_store import QdrantStore


@dataclass
class RagFilters:
    source_type: str | None = None
    character_id: str | None = None
    timeline_id: str | None = None
    world_id: str | None = None
    source_domain: str | None = None

    created_at_gte: str | None = None  # ISO datetime string
    created_at_lte: str | None = None  # ISO datetime string


def to_qdrant_filter(f: RagFilters) -> Optional[qm.Filter]:
    must: List[qm.FieldCondition] = []

    def kw(key: str, val: str | None) -> None:
        if val is None:
            return
        must.append(qm.FieldCondition(key=key, match=qm.MatchValue(value=val)))

    kw("source_type", f.source_type)
    kw("character_id", f.character_id)
    kw("timeline_id", f.timeline_id)
    kw("world_id", f.world_id)
    kw("source_domain", f.source_domain)

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


class RagQueryService:
    def __init__(self, cfg: RagConfig):
        self.cfg = cfg
        self.embedder = Embedder(cfg.embedding_model, device=cfg.embedding_device)
        self.store = QdrantStore(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key,
            collection=cfg.qdrant_collection,
            vector_dim=self.embedder.dim,
        )
        self.store.ensure_collection()

    def search_text(
        self,
        query_text: str,
        *,
        top_k: int | None = None,
        filters: RagFilters | None = None,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        v = self.embedder.embed([query_text])[0]
        qf = to_qdrant_filter(filters or RagFilters())
        return self.store.search_by_vector(
            v,
            top_k=top_k or self.cfg.default_top_k,
            qdrant_filter=qf,
        )

    def search_by_section(
        self,
        section_id: str,
        *,
        top_k: int | None = None,
        filters: RagFilters | None = None,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        v = self.store.get_vector(section_id)
        qf = to_qdrant_filter(filters or RagFilters())
        return self.store.search_by_vector(
            v,
            top_k=top_k or self.cfg.default_top_k,
            qdrant_filter=qf,
        )
