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
    legacy_rag_collection: str = os.getenv(
        "AIOS_QDRANT_LEGACY_RAG_COLLECTION", "doc_sections_v1"
    )
    drop_legacy_rag_collection: bool = os.getenv(
        "AIOS_DROP_LEGACY_RAG_COLLECTION", "1"
    ) == "1"

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

    # Semantic clustering. Core edges create dense components; weaker edges may
    # attach fringe members but cannot merge two established cores.
    cluster_core_threshold: float = float(
        os.getenv("AIOS_SEMANTIC_CLUSTER_CORE_THRESHOLD", "0.82")
    )
    cluster_attach_threshold: float = float(
        os.getenv("AIOS_SEMANTIC_CLUSTER_ATTACH_THRESHOLD", "0.76")
    )
    cluster_boundary_floor: float = float(
        os.getenv("AIOS_SEMANTIC_CLUSTER_BOUNDARY_FLOOR", "0.72")
    )
    cluster_min_size: int = int(
        os.getenv("AIOS_SEMANTIC_CLUSTER_MIN_SIZE", "3")
    )
    cluster_min_attach_links: int = int(
        os.getenv("AIOS_SEMANTIC_CLUSTER_MIN_ATTACH_LINKS", "2")
    )
    cluster_min_density: float = float(
        os.getenv("AIOS_SEMANTIC_CLUSTER_MIN_DENSITY", "0.35")
    )
    cluster_min_cohesion: float = float(
        os.getenv("AIOS_SEMANTIC_CLUSTER_MIN_COHESION", "0.78")
    )

    # Advisory semantic classifier. Low-margin decisions remain UNRESOLVED.
    classifier_min_confidence: float = float(
        os.getenv("AIOS_SEMANTIC_CLASSIFIER_MIN_CONFIDENCE", "0.48")
    )
    classifier_min_margin: float = float(
        os.getenv("AIOS_SEMANTIC_CLASSIFIER_MIN_MARGIN", "0.04")
    )
