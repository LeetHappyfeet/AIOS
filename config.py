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

    runner_poll_interval: float = float(
        os.getenv("AIOS_RUNNER_POLL_INTERVAL", "1.0")
    )


settings = Settings()
