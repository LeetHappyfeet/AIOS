# accumulator/ingest/jsonl_ingestor.py

from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path
from uuid import uuid4, UUID
from typing import Iterable

from aios_app.db import Database
from aios_app.dag import get_or_create_timeline, add_node_and_edge

logger = logging.getLogger("accumulator.ingest.jsonl")


class JSONLDAGIngestor:
    """
    Ingests accumulator JSONL records into:
    - DAG (document + paragraph nodes)
    - source_document (projection)
    """

    def __init__(self, db: Database, input_dir: Path):
        self.db = db
        self.input_dir = input_dir

    # -------------------------------------------------
    # Public entry
    # -------------------------------------------------

    async def ingest_all(self) -> None:
        files = sorted(self.input_dir.glob("**/*.jsonl"))
        logger.info("Found %d jsonl files", len(files))

        for path in files:
            await self._ingest_file(path)

    # -------------------------------------------------
    # File ingestion
    # -------------------------------------------------

    async def _ingest_file(self, path: Path) -> None:
        logger.info("Ingesting %s", path)

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                await self._ingest_record(record)

    # -------------------------------------------------
    # Record ingestion
    # -------------------------------------------------

    async def _ingest_record(self, record: dict) -> None:
        url = record["url"]
        content = record["content"]["text"]

        paragraphs = self._split_paragraphs(content)
        if not paragraphs:
            logger.warning("Empty document: %s", url)
            return

        # -------------------------------------------------
        # Create source_document
        # -------------------------------------------------

        document_id = uuid4()

        self.db.execute_sync(
            """
            INSERT INTO aios.source_document (
                document_id,
                source_type,
                source_url,
                title,
                retrieved_at,
                raw_content,
                meta
            )
            VALUES ($1, $2, $3, $4, now(), $5, $6::jsonb)
            """,
            document_id,
            record["source_type"],
            url,
            record.get("content", {}).get("title"),
            "\n\n".join(paragraphs),
            json.dumps({
                "accumulator_id": record["accumulator_id"],
                "jsonl_sha256": record["raw"]["text_sha256"],
            }),
        )

        # -------------------------------------------------
        # Create DAG timeline
        # -------------------------------------------------

        timeline_id = await get_or_create_timeline(
            self.db,
            world_id=self._world_uuid(record),
            session_id=UUID(int=0),
            character_id=None,
            user_name=None,
            scope_key=f"web:{url}",
            meta={"source": "accumulator"},
        )

        # -------------------------------------------------
        # Insert document node
        # -------------------------------------------------

        doc_node_id, _ = await add_node_and_edge(
            self.db,
            timeline_id=timeline_id,
            event_id=self._event_id(),
            kind="document",
            speaker_id=None,
            speaker_role=None,
            recipient_id=None,
            message_text=None,
            payload={
                "document_id": str(document_id),
                "url": url,
                "source": record["accumulator_id"],
            },
        )

        # -------------------------------------------------
        # Insert paragraph nodes
        # -------------------------------------------------

        parent = doc_node_id
        for idx, text in enumerate(paragraphs):
            node_id, _ = await add_node_and_edge(
                self.db,
                timeline_id=timeline_id,
                event_id=self._event_id(),
                kind="paragraph",
                speaker_id=None,
                speaker_role=None,
                recipient_id=None,
                message_text=text,
                payload={
                    "document_id": str(document_id),
                    "paragraph_index": idx,
                    "paragraph_sha256": self._sha(text),
                },
            )
            parent = node_id

        logger.info(
            "Ingested document %s (%d paragraphs)",
            document_id,
            len(paragraphs),
        )

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------

    def _split_paragraphs(self, text: str) -> list[str]:
        return [p.strip() for p in text.split("\n\n") if p.strip()]

    def _sha(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _event_id(self) -> int:
        return self.db.next_event_id_sync()

    def _world_uuid(self, record: dict) -> UUID:
        # Placeholder: can map fandoms / universes later
        return UUID("00000000-0000-0000-0000-000000000000")