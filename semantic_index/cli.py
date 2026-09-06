from __future__ import annotations

import asyncio
import logging

from aios_app.config import settings
from aios_app.db import Database
from .config import SemanticIndexConfig
from .service import index_once, initialize_backend
from .structure import analyze_neighbors_once
from .clustering import cluster_neighbors_once

logger = logging.getLogger("aios.semantic_index.cli")


async def run_forever(poll_seconds: float = 1.0) -> None:
    cfg = SemanticIndexConfig()
    db = Database(settings.db_dsn)
    await db.connect()
    try:
        logger.info("Semantic Index startup: initializing embedding backend and Qdrant collections")
        initialize_backend(cfg, warmup=True)
        logger.info("AIOS_READY service=semantic_index")
        while True:
            indexed = await index_once(db, cfg)
            structured = await analyze_neighbors_once(db, cfg)
            clustered = await cluster_neighbors_once(db, cfg)
            if indexed == 0 and structured == 0 and clustered == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())
