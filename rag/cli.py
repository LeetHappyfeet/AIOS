# aios_app/rag/cli.py

from __future__ import annotations
import asyncio
import logging
import time

from aios_app.db import Database
from aios_app.config import settings  # your existing settings object
from .rag_config import RagConfig
from .ingest_worker import ingest_once

logger = logging.getLogger("aios.rag.cli")


async def run_once() -> int:
    cfg = RagConfig()
    db = Database(settings.db_dsn)
    await db.connect()
    try:
        n = await ingest_once(db, cfg)
        return n
    finally:
        await db.close()


async def run_forever(poll_seconds: int = 2) -> None:
    cfg = RagConfig()
    db = Database(settings.db_dsn)
    await db.connect()
    logger.info("AIOS_READY service=rag")
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
