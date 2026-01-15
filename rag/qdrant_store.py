# aios_app/rag/qdrant_store.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm


@dataclass
class QdrantStore:
    url: str
    api_key: str | None
    collection: str
    vector_dim: int

    def __post_init__(self) -> None:
        self.client = QdrantClient(url=self.url, api_key=self.api_key)

    def ensure_collection(self) -> None:
        # Create if missing; if exists, we trust its schema matches.
        existing = None
        try:
            existing = self.client.get_collection(self.collection)
        except Exception:
            existing = None

        if existing is not None:
            return

        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=qm.VectorParams(
                size=self.vector_dim,
                distance=qm.Distance.COSINE,
            ),
        )

        # Helpful payload indexes for filters (cheap)
        # NOTE: Qdrant ignores duplicates; safe to call.
        for field, schema in [
            ("source_type", qm.PayloadSchemaType.KEYWORD),
            ("character_id", qm.PayloadSchemaType.KEYWORD),
            ("timeline_id", qm.PayloadSchemaType.KEYWORD),
            ("world_id", qm.PayloadSchemaType.KEYWORD),
            ("source_domain", qm.PayloadSchemaType.KEYWORD),
            ("created_at", qm.PayloadSchemaType.DATETIME),
        ]:
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception:
                pass

    def upsert_points(self, points: List[qm.PointStruct]) -> None:
        self.client.upsert(
            collection_name=self.collection,
            points=points,
            wait=True,
        )

    def search_by_vector(
        self,
        query_vector: List[float],
        *,
        top_k: int,
        qdrant_filter: Optional[qm.Filter] = None,
        with_payload: bool = True,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        res = self.client.search(
            collection_name=self.collection,
            query_vector=query_vector,
            limit=top_k,
            query_filter=qdrant_filter,
            with_payload=with_payload,
            with_vectors=False,
        )
        out: List[Tuple[str, float, Dict[str, Any]]] = []
        for p in res:
            pid = str(p.id)
            payload = dict(p.payload or {})
            out.append((pid, float(p.score), payload))
        return out

    def get_vector(self, point_id: str) -> List[float]:
        pts = self.client.retrieve(
            collection_name=self.collection,
            ids=[point_id],
            with_vectors=True,
            with_payload=False,
        )
        if not pts:
            raise KeyError(f"Point not found: {point_id}")
        v = pts[0].vector
        # vector can be dict if named vectors used; we only use default
        if isinstance(v, dict):
            # pick first value
            return list(next(iter(v.values())))
        return list(v)  # type: ignore
