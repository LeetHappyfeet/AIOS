from pydantic import BaseModel
import os
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseModel):
    db_dsn: str = os.getenv("AIOS_DB_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")
    api_host: str = os.getenv("AIOS_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("AIOS_API_PORT", "8000"))
    source_name: str = os.getenv("AIOS_SOURCE_NAME", "SillyTavern")
    default_scope: str = os.getenv("AIOS_DEFAULT_SCOPE", "default")


settings = Settings()