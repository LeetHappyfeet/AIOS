# aios_app/rag/claims/claims_cli.py
from __future__ import annotations

import asyncio
import logging

from aios_app.db import Database
from aios_app.config import settings
from ..rag_config import RagConfig
from .claim_ingest_worker import ingest_once

logger = logging.getLogger("aios.rag.claims.cli")


def _claims_cfg() -> RagConfig:
    """
    Build a RagConfig explicitly for claim ingestion.

    This intentionally overrides only the collection name.
    All other embedding / batching / device settings are inherited.
    """
    return RagConfig(
        qdrant_collection="claims_v1",
    )


async def run_once() -> int:
    cfg = _claims_cfg()
    db = Database(settings.db_dsn)
    await db.connect()
    try:
        n = await ingest_once(db, cfg)
        return n
    finally:
        await db.close()


async def run_forever(poll_seconds: int = 2) -> None:
    cfg = _claims_cfg()
    db = Database(settings.db_dsn)
    await db.connect()
    try:
        while True:
            n = await ingest_once(db, cfg)
            if n == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())
