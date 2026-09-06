from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm


PAYLOAD_INDEXES = {
    "object_type": qm.PayloadSchemaType.KEYWORD,
    "section_id": qm.PayloadSchemaType.KEYWORD,
    "proposition_id": qm.PayloadSchemaType.KEYWORD,
    "claim_kind": qm.PayloadSchemaType.KEYWORD,
    "predicate_family": qm.PayloadSchemaType.KEYWORD,
    "character_id": qm.PayloadSchemaType.KEYWORD,
    "instance_id": qm.PayloadSchemaType.KEYWORD,
    "world_id": qm.PayloadSchemaType.KEYWORD,
    "timeline_id": qm.PayloadSchemaType.KEYWORD,
    "source_type": qm.PayloadSchemaType.KEYWORD,
    "source_domain": qm.PayloadSchemaType.KEYWORD,
    "epistemic_status": qm.PayloadSchemaType.KEYWORD,
    "created_at": qm.PayloadSchemaType.DATETIME,
}


@dataclass
class QdrantStore:
    url: str
    api_key: str | None
    collection: str
    vector_dim: int

    def __post_init__(self) -> None:
        self.client = QdrantClient(url=self.url, api_key=self.api_key)

    def ensure_collection(self) -> None:
        try:
            self.client.get_collection(self.collection)
            exists = True
        except Exception:
            exists = False
        if not exists:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=qm.VectorParams(
                    size=self.vector_dim,
                    distance=qm.Distance.COSINE,
                ),
            )
        for field, schema in PAYLOAD_INDEXES.items():
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception:
                pass

    def upsert(self, points: list[qm.PointStruct]) -> None:
        if points:
            self.client.upsert(
                collection_name=self.collection,
                points=points,
                wait=True,
            )

    def search(
        self,
        vector: list[float],
        *,
        top_k: int,
        qdrant_filter: qm.Filter | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        hits = self.client.search(
            collection_name=self.collection,
            query_vector=vector,
            limit=top_k,
            query_filter=qdrant_filter,
            with_payload=True,
            with_vectors=False,
        )
        return [(str(h.id), float(h.score), dict(h.payload or {})) for h in hits]

    def vector(self, point_id: str) -> list[float]:
        rows = self.client.retrieve(
            collection_name=self.collection,
            ids=[point_id],
            with_vectors=True,
            with_payload=False,
        )
        if not rows:
            raise KeyError(point_id)
        value = rows[0].vector
        if isinstance(value, dict):
            value = next(iter(value.values()))
        return list(value)  # type: ignore[arg-type]
