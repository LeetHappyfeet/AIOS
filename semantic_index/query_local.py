from __future__ import annotations

from typing import Any, Iterable

from qdrant_client.http import models as qm

from .config import SemanticIndexConfig
from .service import _get_embedder, _get_store


class LocalSemanticQueryService:
    """In-process semantic query implementation owned by Semantic Index.

    This class is intentionally not used by the API/HUD process.  It reuses
    the already-warm embedder and Qdrant stores initialized by Semantic Index.
    """

    def __init__(self, cfg: SemanticIndexConfig | None = None):
        self.cfg = cfg or SemanticIndexConfig()
        self.embedder = _get_embedder(self.cfg)

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
        return _get_store(self.cfg, collection).search(
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
