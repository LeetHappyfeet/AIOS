# aios_app/rag/qdrant_store.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

logger = logging.getLogger("aios.rag.store")


@dataclass
class QdrantStore:
    """
    Thin wrapper around Qdrant collections used by AIOS RAG.

    Version-adaptive search compatible with older and newer qdrant-client APIs.
    """

    url: str
    api_key: Optional[str]
    collection: str
    vector_dim: int

    def __post_init__(self) -> None:
        self.client = QdrantClient(
            url=self.url,
            api_key=self.api_key,
        )

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection):
            logger.info(
                "Creating Qdrant collection [%s] (dim=%d)",
                self.collection,
                self.vector_dim,
            )
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=qm.VectorParams(
                    size=self.vector_dim,
                    distance=qm.Distance.COSINE,
                ),
            )

        for field, schema in [
            ("source_type", qm.PayloadSchemaType.KEYWORD),
            ("character_id", qm.PayloadSchemaType.KEYWORD),
            ("timeline_id", qm.PayloadSchemaType.KEYWORD),
            ("world_id", qm.PayloadSchemaType.KEYWORD),
            ("world_key", qm.PayloadSchemaType.KEYWORD),
            ("status", qm.PayloadSchemaType.KEYWORD),
            ("extraction_ver", qm.PayloadSchemaType.KEYWORD),
            ("embedding_model", qm.PayloadSchemaType.KEYWORD),
            ("embedding_version", qm.PayloadSchemaType.KEYWORD),
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

    def upsert_points(self, points: Iterable[qm.PointStruct]) -> None:
        self.client.upsert(
            collection_name=self.collection,
            points=list(points),
        )

    def search(
        self,
        *,
        vector: List[float],
        limit: int = 10,
        filter: Optional[qm.Filter] = None,
    ) -> List[qm.ScoredPoint]:
        """
        Version-adaptive similarity search supporting:
          - query_points(collection_name, query=vector)
          - search_points (mid-era qdrant-client)
          - search (legacy qdrant-client)
        """

        # -----------------------------------------------------
        # 1) Attempt query_points (newest API variant)
        #    Only passes the required args accepted by your client
        # -----------------------------------------------------
        if hasattr(self.client, "query_points"):
            try:
                # This variant supports only collection_name and query
                # No extra kwargs (payload, vectors, filter)
                resp = self.client.query_points(
                    collection_name=self.collection,
                    query=vector,
                )
                # resp is a QueryResponse; .points holds ScoredPoints
                return list(resp.points)
            except AssertionError:
                # If unsupported signature, fall through
                pass

        # -----------------------------------------------------
        # 2) Mid-era clients
        #    search_points accepts vector + filter + payload flags
        # -----------------------------------------------------
        if hasattr(self.client, "search_points"):
            return self.client.search_points(
                collection_name=self.collection,
                vector=vector,
                limit=limit,
                query_filter=filter,
                with_payload=True,
                with_vectors=False,
            )

        # -----------------------------------------------------
        # 3) Legacy clients
        # -----------------------------------------------------
        if hasattr(self.client, "search"):
            return self.client.search(
                collection_name=self.collection,
                query_vector=vector,
                limit=limit,
                query_filter=filter,
                with_payload=True,
                with_vectors=False,
            )

        raise RuntimeError(
            "Unsupported qdrant-client version: no compatible search method found"
        )
