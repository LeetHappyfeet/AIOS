# aios_app/rag/rag_config.py

from __future__ import annotations
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class RagConfig:
    # Qdrant
    qdrant_url: str = os.getenv("AIOS_QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_api_key: str | None = os.getenv("AIOS_QDRANT_API_KEY") or None
    qdrant_collection: str = os.getenv("AIOS_QDRANT_COLLECTION", "doc_sections_v1")

    # Embeddings
    embedding_model: str = os.getenv("AIOS_EMBEDDING_MODEL", "sentence-transformers/multi-qa-mpnet-base-dot-v1")
    embedding_version: str = os.getenv("AIOS_EMBEDDING_VERSION", "v1")
    embedding_device: str | None = os.getenv("AIOS_EMBEDDING_DEVICE") or None  # "cpu", "cuda", or None(auto)

    # Ingestion
    batch_size: int = int(os.getenv("AIOS_RAG_BATCH_SIZE", "64"))
    reindex_on_hash_change: bool = os.getenv("AIOS_RAG_REINDEX_HASH", "1") == "1"

    # Query defaults
    default_top_k: int = int(os.getenv("AIOS_RAG_TOP_K", "30"))

    # Split detection defaults (tune later)
    edge_threshold: float = float(os.getenv("AIOS_RAG_T_EDGE", "0.83"))
    max_nodes: int = int(os.getenv("AIOS_RAG_SPLIT_MAX_NODES", "500"))
