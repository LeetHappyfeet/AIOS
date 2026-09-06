# accumulator/main.py

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from aios_app.config import settings
from aios_app.db import Database

from .ingest.jsonl_ingestor import JSONLDAGIngestor
from .web.config import OUTPUT_DIR
from .sillytavern.config import (
    INPUT_DIR as SILLYTAVERN_INPUT_DIR,
    STATE_FILE as SILLYTAVERN_STATE_FILE,
)
from .sillytavern.ingestor import SillyTavernChatIngestor
from .sillytavern.parser import parse_sillytavern_jsonl

logger = logging.getLogger("accumulator.main")


# =================================================
# Ingest state (JSONL offsets)
# =================================================

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
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )


# =================================================
# Main ingest loop (UNCHANGED LOGIC)
# =================================================

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
            except Exception:
                logger.exception("Unhandled error ingesting %s", path)
                continue

            if end_at != start_at:
                state.offsets[key] = end_at
                state.save(state_file)

        await asyncio.sleep(interval)


# =================================================
# SillyTavern JSONL chat-log loop
# =================================================

async def sillytavern_ingest_loop(
    ingestor: SillyTavernChatIngestor,
    input_dir: Path,
    state_file: Path,
    interval: float,
) -> None:
    if state_file.exists():
        try:
            raw_state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            raw_state = {}
    else:
        raw_state = {}

    processed = {
        str(key): str(value)
        for key, value in raw_state.get("processed", {}).items()
    }
    failed = {
        str(key): str(value)
        for key, value in raw_state.get("failed", {}).items()
    }

    def save_state() -> None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {"processed": processed, "failed": failed},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    while True:
        for path in sorted(input_dir.glob("*.jsonl")):
            key = str(path)
            try:
                parsed = parse_sillytavern_jsonl(path)
            except Exception as exc:
                signature = f"{path.stat().st_size}:{path.stat().st_mtime_ns}"
                if failed.get(key) != signature:
                    logger.exception("Invalid SillyTavern chat log %s", path)
                    failed[key] = signature
                    save_state()
                continue

            if processed.get(key) == parsed.file_sha256:
                continue

            try:
                result = await ingestor.ingest_file(path)
            except Exception:
                logger.exception("Failed to ingest SillyTavern chat log %s", path)
                continue

            processed[key] = parsed.file_sha256
            failed.pop(key, None)
            save_state()
            logger.info(
                "SillyTavern import complete: %s messages=%s timeline=%s",
                path.name,
                result["messages"],
                result["timeline_id"],
            )

        await asyncio.sleep(interval)


# =================================================
# Accumulator service entrypoint
# =================================================

async def run_accumulator() -> None:
    logging.basicConfig(level=logging.INFO)

    input_dir = Path(os.getenv(
        "ACCUMULATOR_INPUT_DIR",
        str(OUTPUT_DIR),
    ))

    state_file = Path(os.getenv(
        "ACCUMULATOR_STATE_FILE",
        str(input_dir / ".ingest_state.json"),
    ))

    ingest_interval = float(os.getenv(
        "ACCUMULATOR_INGEST_INTERVAL",
        "5.0",
    ))

    sillytavern_input_dir = Path(os.getenv(
        "SILLYTAVERN_ACCUMULATOR_INPUT_DIR",
        str(SILLYTAVERN_INPUT_DIR),
    ))
    sillytavern_state_file = Path(os.getenv(
        "SILLYTAVERN_ACCUMULATOR_STATE_FILE",
        str(SILLYTAVERN_STATE_FILE),
    ))

    input_dir.mkdir(parents=True, exist_ok=True)
    sillytavern_input_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Accumulator input dir: %s", input_dir)
    logger.info("Accumulator state file: %s", state_file)
    logger.info("Accumulator ingest interval: %.2fs", ingest_interval)
    logger.info("SillyTavern input dir: %s", sillytavern_input_dir)
    logger.info("SillyTavern state file: %s", sillytavern_state_file)

    db = Database(settings.db_dsn)
    await db.connect()

    try:
        ingestor = JSONLDAGIngestor(db, input_dir)
        sillytavern_ingestor = SillyTavernChatIngestor(db)
        logger.info("AIOS_READY service=accumulator")
        await asyncio.gather(
            ingest_loop(
                ingestor=ingestor,
                input_dir=input_dir,
                state_file=state_file,
                interval=ingest_interval,
            ),
            sillytavern_ingest_loop(
                ingestor=sillytavern_ingestor,
                input_dir=sillytavern_input_dir,
                state_file=sillytavern_state_file,
                interval=ingest_interval,
            ),
        )
    finally:
        await db.close()
        logger.info("Accumulator shutdown complete")


# =================================================
# CLI entrypoint
# =================================================

if __name__ == "__main__":
    asyncio.run(run_accumulator())
