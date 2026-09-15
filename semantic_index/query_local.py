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

    @staticmethod
    def _filter(
        *,
        must: dict[str, Any] | None = None,
        any_values: dict[str, Iterable[Any]] | None = None,
    ) -> qm.Filter | None:
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
        return qm.Filter(must=conditions) if conditions else None

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
        qfilter = self._filter(must=must, any_values=any_values)
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
        world_ids: Iterable[Any] | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        any_values: dict[str, Iterable[Any]] = {"instance_id": instance_ids}
        if world_ids is not None:
            any_values["world_id"] = world_ids
        return self.search(
            query_text,
            collection=self.cfg.epistemic_collection,
            top_k=top_k or self.cfg.hud_candidate_k,
            must={"object_type": "character_knowledge", "character_id": character_id},
            any_values=any_values,
        )

    def search_epistemic_staged(
        self,
        query_text: str,
        *,
        character_id: str,
        instance_ids: Iterable[Any],
        world_stages: Iterable[Iterable[Any]],
        top_k: int | None = None,
        min_hits: int = 8,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """Local-first Qdrant retrieval with one embedding computation.

        World priority is intentionally not blended into cosine similarity.
        Earlier stages are searched first; later stages are queried only if the
        earlier canon did not provide enough unique semantic candidates.
        """
        if not query_text.strip():
            return []

        stages = [
            tuple(str(value) for value in stage if value is not None)
            for stage in world_stages
        ]
        stages = [stage for stage in stages if stage]
        if not stages:
            return []

        vector = self.embedder.embed([query_text])[0]
        store = _get_store(self.cfg, self.cfg.epistemic_collection)
        candidate_k = top_k or self.cfg.hud_candidate_k
        required = max(1, int(min_hits))
        instance_values = tuple(str(value) for value in instance_ids if value is not None)

        merged: list[tuple[str, float, dict[str, Any]]] = []
        seen_ids: set[str] = set()
        for world_ids in stages:
            qfilter = self._filter(
                must={
                    "object_type": "character_knowledge",
                    "character_id": character_id,
                },
                any_values={
                    "instance_id": instance_values,
                    "world_id": world_ids,
                },
            )
            hits = store.search(
                vector,
                top_k=candidate_k,
                qdrant_filter=qfilter,
            )
            for hit in hits:
                point_id = str(hit[0])
                if point_id in seen_ids:
                    continue
                seen_ids.add(point_id)
                merged.append(hit)
            if len(merged) >= required:
                break

        return merged[:candidate_k]
