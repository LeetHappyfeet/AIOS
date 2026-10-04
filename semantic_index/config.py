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
    frame_collection: str = os.getenv(
        "AIOS_QDRANT_FRAME_COLLECTION", "semantic_frames_v2"
    )
    proposition_collection: str = os.getenv(
        "AIOS_QDRANT_PROPOSITION_COLLECTION", "propositions_v1"
    )
    epistemic_collection: str = os.getenv(
        "AIOS_QDRANT_EPISTEMIC_COLLECTION", "epistemic_objects_v1"
    )
    topic_collection: str = os.getenv(
        "AIOS_QDRANT_TOPIC_COLLECTION", "knowledge_topics_v1"
    )
    corpus_collection: str = os.getenv(
        "AIOS_QDRANT_CORPUS_COLLECTION", "corpus_sections_v2"
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

    # Low-latency query RPC. The Semantic Index process owns the warm embedding
    # model; API/HUD callers connect over loopback and never load it locally.
    query_host: str = os.getenv("AIOS_SEMANTIC_QUERY_HOST", "127.0.0.1")
    query_port: int = int(os.getenv("AIOS_SEMANTIC_QUERY_PORT", "8765"))
    query_timeout_seconds: float = float(
        os.getenv("AIOS_SEMANTIC_QUERY_TIMEOUT_SECONDS", "0.20")
    )

    batch_size: int = int(os.getenv("AIOS_SEMANTIC_INDEX_BATCH_SIZE", "64"))
    # Independent budgets keep topology maintenance below the vector workload.
    neighbor_batch_size: int = max(1, int(os.getenv(
        "AIOS_SEMANTIC_NEIGHBOR_BATCH_SIZE", "8")))
    relation_batch_size: int = max(1, int(os.getenv(
        "AIOS_SEMANTIC_RELATION_BATCH_SIZE", "16")))
    validation_batch_size: int = max(1, int(os.getenv(
        "AIOS_SEMANTIC_VALIDATION_BATCH_SIZE", str(max(100, batch_size * 4)))))
    reconciliation_batch_size: int = max(1, int(os.getenv(
        "AIOS_SEMANTIC_RECONCILIATION_BATCH_SIZE", str(batch_size))))
    background_batch_size: int = max(1, int(os.getenv("AIOS_SEMANTIC_BACKGROUND_BATCH_SIZE", "8")))
    admission_batch_size: int = max(1, int(os.getenv("AIOS_SEMANTIC_ADMISSION_BATCH_SIZE", "8")))
    vector_sql_seconds: float = max(0.1, float(os.getenv("AIOS_SEMANTIC_VECTOR_SQL_SECONDS", "10")))
    admission_stage_seconds: float = max(1.0, float(os.getenv("AIOS_SEMANTIC_ADMISSION_STAGE_SECONDS", "10")))
    topology_stage_seconds: float = max(1.0, float(os.getenv("AIOS_SEMANTIC_TOPOLOGY_STAGE_SECONDS", "10")))
    topology_sql_seconds: float = max(0.1, float(os.getenv("AIOS_SEMANTIC_TOPOLOGY_SQL_SECONDS", "5")))
    default_top_k: int = int(os.getenv("AIOS_SEMANTIC_TOP_K", "80"))
    hud_candidate_k: int = int(os.getenv("AIOS_SEMANTIC_HUD_CANDIDATE_K", "200"))
    neighbor_refresh_seconds: float = float(os.getenv("AIOS_SEMANTIC_NEIGHBOR_REFRESH_SECONDS", "3600"))
    neighbor_k: int = int(os.getenv("AIOS_SEMANTIC_NEIGHBOR_K", "12"))
    neighbor_min_score: float = float(os.getenv("AIOS_SEMANTIC_NEIGHBOR_MIN_SCORE", "0.72"))

    cluster_min_interval_seconds: float = float(os.getenv("AIOS_SEMANTIC_CLUSTER_MIN_INTERVAL_SECONDS", "30"))
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

    classifier_min_confidence: float = float(
        os.getenv("AIOS_SEMANTIC_CLASSIFIER_MIN_CONFIDENCE", "0.48")
    )
    classifier_min_margin: float = float(
        os.getenv("AIOS_SEMANTIC_CLASSIFIER_MIN_MARGIN", "0.04")
    )

    reconcile_relation_min_confidence: float = float(
        os.getenv("AIOS_SEMANTIC_RECONCILE_RELATION_MIN_CONFIDENCE", "0.80")
    )
    reconcile_cluster_min_confidence: float = float(
        os.getenv("AIOS_SEMANTIC_RECONCILE_CLUSTER_MIN_CONFIDENCE", "0.60")
    )
    reconcile_boundary_min_confidence: float = float(
        os.getenv("AIOS_SEMANTIC_RECONCILE_BOUNDARY_MIN_CONFIDENCE", "0.60")
    )
