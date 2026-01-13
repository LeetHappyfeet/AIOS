# accumulator/main.py

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from .ingest.jsonl_ingestor import JSONLDAGIngestor
from .web.config import OUTPUT_DIR
from .web.gradio_ui import demo
from aios_app.config import settings
from aios_app.db import Database

logger = logging.getLogger("accumulator.main")


@dataclass
class IngestState:
    offsets: Dict[str, int]

    @classmethod
    def load(cls, path: Path) -> "IngestState":
        if not path.exists():
            return cls(offsets={})
        data = json.loads(path.read_text(encoding="utf-8"))
        offsets = {key: int(value) for key, value in data.get("offsets", {}).items()}
        return cls(offsets=offsets)

    def save(self, path: Path) -> None:
        payload = {"offsets": self.offsets}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


async def ingest_loop(
    ingestor: JSONLDAGIngestor,
    input_dir: Path,
    state_file: Path,
    interval: float,
) -> None:
    state = IngestState.load(state_file)

    while True:
        files = sorted(input_dir.glob("**/*.jsonl"))
        if files:
            logger.info("Scanning %d jsonl files for ingestion", len(files))

        for path in files:
            key = str(path)
            start_at = state.offsets.get(key, 0)
            try:
                end_at = await ingestor.ingest_file(path, start_at=start_at)
            except json.JSONDecodeError:
                logger.exception("Failed to decode JSONL line in %s", path)
                continue

            if end_at != start_at:
                state.offsets[key] = end_at
                state.save(state_file)

        await asyncio.sleep(interval)


async def run_accumulator() -> None:
    logging.basicConfig(level=logging.INFO)

    input_dir = Path(os.getenv("ACCUMULATOR_INPUT_DIR", str(OUTPUT_DIR)))
    state_file = Path(
        os.getenv("ACCUMULATOR_STATE_FILE", str(input_dir / ".ingest_state.json"))
    )
    ingest_interval = float(os.getenv("ACCUMULATOR_INGEST_INTERVAL", "5.0"))
    gradio_host = os.getenv("ACCUMULATOR_GRADIO_HOST", "0.0.0.0")
    gradio_port = int(os.getenv("ACCUMULATOR_GRADIO_PORT", "7860"))

    input_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Accumulator input dir: %s", input_dir)
    logger.info("Accumulator state file: %s", state_file)

    db = Database(settings.db_dsn)
    await db.connect()

    try:
        demo.launch(
            server_name=gradio_host,
            server_port=gradio_port,
            prevent_thread_lock=True,
        )

        ingestor = JSONLDAGIngestor(db, input_dir)
        await ingest_loop(ingestor, input_dir, state_file, ingest_interval)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(run_accumulator())
