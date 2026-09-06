from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class SemanticIndexConfig:
    qdrant_url: str = os.getenv("AIOS_QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_api_key: str | None = os.getenv("AIOS_QDRANT_API_KEY") or None

    source_collection: str = os.getenv(
        "AIOS_QDRANT_SOURCE_COLLECTION", "source_sections_v1"
    )
    proposition_collection: str = os.getenv(
        "AIOS_QDRANT_PROPOSITION_COLLECTION", "propositions_v1"
    )
    epistemic_collection: str = os.getenv(
        "AIOS_QDRANT_EPISTEMIC_COLLECTION", "epistemic_objects_v1"
    )

    embedding_model: str = os.getenv(
        "AIOS_SEMANTIC_EMBEDDING_MODEL",
        os.getenv("AIOS_EMBEDDING_MODEL", "sentence-transformers/multi-qa-mpnet-base-dot-v1"),
    )
    embedding_version: str = os.getenv("AIOS_SEMANTIC_EMBEDDING_VERSION", "v1")
    embedding_device: str | None = os.getenv(
        "AIOS_SEMANTIC_EMBEDDING_DEVICE",
        os.getenv("AIOS_EMBEDDING_DEVICE", ""),
    ) or None

    batch_size: int = int(os.getenv("AIOS_SEMANTIC_INDEX_BATCH_SIZE", "64"))
    default_top_k: int = int(os.getenv("AIOS_SEMANTIC_TOP_K", "80"))
    hud_candidate_k: int = int(os.getenv("AIOS_SEMANTIC_HUD_CANDIDATE_K", "200"))
    neighbor_k: int = int(os.getenv("AIOS_SEMANTIC_NEIGHBOR_K", "24"))
    neighbor_min_score: float = float(os.getenv("AIOS_SEMANTIC_NEIGHBOR_MIN_SCORE", "0.72"))
