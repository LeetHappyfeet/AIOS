# aios/config.py

from pydantic import BaseModel
import os
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseModel):
    # -------------------------------------------------
    # Database
    # -------------------------------------------------
    db_dsn: str = os.getenv(
        "AIOS_DB_DSN",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )

    # -------------------------------------------------
    # API
    # -------------------------------------------------
    api_host: str = os.getenv("AIOS_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("AIOS_API_PORT", "8000"))

    # -------------------------------------------------
    # Ingest / source
    # -------------------------------------------------
    source_name: str = os.getenv("AIOS_SOURCE_NAME", "SillyTavern")
    default_scope: str = os.getenv("AIOS_DEFAULT_SCOPE", "conversation")

    # -------------------------------------------------
    # RDF / Fuseki
    # -------------------------------------------------
    fuseki_base_url: str = os.getenv(
        "AIOS_FUSEKI_BASE_URL",
        "http://127.0.0.1:3030",
    )

    fuseki_timeout: float = float(
        os.getenv("AIOS_FUSEKI_TIMEOUT", "15.0")
    )

    fuseki_retries: int = int(
        os.getenv("AIOS_FUSEKI_RETRIES", "2")
    )

    # -------------------------------------------------
    # World resolution
    # -------------------------------------------------
    default_world_key: str = os.getenv(
        "AIOS_DEFAULT_WORLD_KEY",
        "liminal",
    )

    # -------------------------------------------------
    # Supervisor
    # -------------------------------------------------
    supervisor_poll_interval: float = float(
        os.getenv("AIOS_SUPERVISOR_POLL_INTERVAL", "2.0")
    )

    supervisor_batch_size: int = int(
        os.getenv("AIOS_SUPERVISOR_BATCH_SIZE", "25")
    )

    supervisor_max_jobs_per_cycle: int = int(
        os.getenv("AIOS_SUPERVISOR_MAX_JOBS_PER_CYCLE", "50")
    )

    supervisor_max_queued_backlog: int = int(
        os.getenv("AIOS_SUPERVISOR_MAX_QUEUED_BACKLOG", "500")
    )

    supervisor_critical_queue_reserve: int = int(
        os.getenv("AIOS_SUPERVISOR_CRITICAL_QUEUE_RESERVE", "128")
    )

    pipeline_stale_running_seconds: int = int(
        os.getenv("AIOS_PIPELINE_STALE_RUNNING_SECONDS", "1800")
    )

    runner_poll_interval: float = float(
        os.getenv("AIOS_RUNNER_POLL_INTERVAL", "1.0")
    )

    pipeline_lease_seconds: int = int(
        os.getenv("AIOS_PIPELINE_LEASE_SECONDS", "120")
    )

    pipeline_heartbeat_seconds: int = int(
        os.getenv("AIOS_PIPELINE_HEARTBEAT_SECONDS", "30")
    )

    runner_fast_sql_workers: int = int(os.getenv("AIOS_RUNNER_FAST_SQL_WORKERS", "4"))
    # Frame decomposition is serialized per source timeline by the queue
    # admission policy, so independent timelines can safely use separate NLP
    # workers without allowing a later claim to outrun its local antecedents.
    runner_nlp_workers: int = int(os.getenv("AIOS_RUNNER_NLP_WORKERS", "4"))
    runner_semantic_workers: int = int(os.getenv("AIOS_RUNNER_SEMANTIC_WORKERS", "4"))
    runner_vector_workers: int = int(os.getenv("AIOS_RUNNER_VECTOR_WORKERS", "1"))
    runner_rdf_workers: int = int(os.getenv("AIOS_RUNNER_RDF_WORKERS", "1"))
    runner_reconciliation_workers: int = int(
        os.getenv("AIOS_RUNNER_RECONCILIATION_WORKERS", "1")
    )
    runner_global_workers: int = int(os.getenv("AIOS_RUNNER_GLOBAL_WORKERS", "1"))

    db_pool_min_size: int = int(os.getenv("AIOS_DB_POOL_MIN_SIZE", "1"))
    db_pool_max_size: int = int(os.getenv("AIOS_DB_POOL_MAX_SIZE", "24"))

settings = Settings()
