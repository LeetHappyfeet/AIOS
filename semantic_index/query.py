from __future__ import annotations

from typing import Any, Iterable
from qdrant_client.http import models as qm

from .config import SemanticIndexConfig
from .embeddings import Embedder
from .store import QdrantStore

_EMBEDDER: Embedder | None = None
_STORES: dict[str, QdrantStore] = {}


def _embedder(cfg: SemanticIndexConfig) -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = Embedder(cfg.embedding_model, cfg.embedding_device)
    return _EMBEDDER


def _store(cfg: SemanticIndexConfig, collection: str) -> QdrantStore:
    if collection not in _STORES:
        emb = _embedder(cfg)
        _STORES[collection] = QdrantStore(
            cfg.qdrant_url, cfg.qdrant_api_key, collection, emb.dim
        )
        _STORES[collection].ensure_collection()
    return _STORES[collection]


class SemanticQueryService:
    def __init__(self, cfg: SemanticIndexConfig | None = None):
        self.cfg = cfg or SemanticIndexConfig()
        self.embedder = _embedder(self.cfg)

    def search(
        self,
        query_text: str,
        *,
        collection: str,
        top_k: int | None = None,
        must: dict[str, Any] | None = None,
        any_values: dict[str, Iterable[Any]] | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        if not query_text.strip():
            return []
        conditions: list[qm.FieldCondition] = []
        for key, value in (must or {}).items():
            if value is not None:
                conditions.append(
                    qm.FieldCondition(key=key, match=qm.MatchValue(value=str(value)))
                )
        for key, values in (any_values or {}).items():
            vals = [str(v) for v in values if v is not None]
            if vals:
                conditions.append(
                    qm.FieldCondition(key=key, match=qm.MatchAny(any=vals))
                )
        qfilter = qm.Filter(must=conditions) if conditions else None
        vector = self.embedder.embed([query_text])[0]
        return _store(self.cfg, collection).search(
            vector,
            top_k=top_k or self.cfg.default_top_k,
            qdrant_filter=qfilter,
        )

    def search_epistemic(
        self,
        query_text: str,
        *,
        character_id: str,
        instance_ids: Iterable[Any],
        top_k: int | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        return self.search(
            query_text,
            collection=self.cfg.epistemic_collection,
            top_k=top_k or self.cfg.hud_candidate_k,
            must={"object_type": "character_knowledge", "character_id": character_id},
            any_values={"instance_id": instance_ids},
        )
