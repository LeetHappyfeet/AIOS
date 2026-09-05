# accumulator/ingest/jsonl_ingestor.py

from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path
from uuid import uuid4

from aios_app.db import Database
from aios_app.dag import get_or_create_timeline, add_node_and_edge

logger = logging.getLogger("accumulator.ingest.jsonl")

# -------------------------------------------------
# Constants
# -------------------------------------------------

LIMINAL_WORLD_KEY = "liminal"
ACCUMULATOR_ACTOR = "accumulator"
SCOPE_KEY = "source:web"


class JSONLDAGIngestor:
    """
    Ingests accumulator JSONL records into:
    - DAG (document + paragraph nodes)
    - source_document (projection)

    Each web page is treated as its own epistemic timeline.
    """

    def __init__(self, db: Database, input_dir: Path):
        self.db = db
        self.input_dir = input_dir

    # -------------------------------------------------
    # Public entry
    # -------------------------------------------------

    async def ingest_file(self, path: Path, *, start_at: int = 0) -> int:
        logger.info("Ingesting %s from offset %s", path, start_at)

        with path.open("r", encoding="utf-8") as f:
            if start_at:
                f.seek(start_at)

            for line in f:
                if not line.strip():
                    continue
                record = json.loads(line)
                await self._ingest_record(record)

            return f.tell()

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

        await self.db.execute(
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
            "internet",
            url,
            record.get("content", {}).get("title"),
            "\n\n".join(paragraphs),
            json.dumps({
                "accumulator_id": record["accumulator_id"],
                "jsonl_sha256": record["raw"]["text_sha256"],
            }),
        )

        # -------------------------------------------------
        # Per-source LIMINAL timeline
        # -------------------------------------------------

        timeline_id = await get_or_create_timeline(
            self.db,
            world_key=LIMINAL_WORLD_KEY,
            session_id=None,
            character_id=ACCUMULATOR_ACTOR,
            user_name=ACCUMULATOR_ACTOR,
            scope_key=f"{SCOPE_KEY}:{record['accumulator_id']}",
            meta={
                "source": "internet",
                "source_url": url,
            },
        )

        # -------------------------------------------------
        # Document root node
        # -------------------------------------------------

        doc_event_id = await self._create_ingest_event(
            record,
            kind="document",
            url=url,
        )

        doc_node_id, _ = await add_node_and_edge(
            self.db,
            timeline_id=timeline_id,
            event_id=doc_event_id,
            character_id=ACCUMULATOR_ACTOR,
            kind="document",
            speaker_id=ACCUMULATOR_ACTOR,
            speaker_role="system",
            recipient_id=None,
            message_text=None,
            payload={
                "document_id": str(document_id),
                "url": url,
                "source": "internet",
            },
        )

        # -------------------------------------------------
        # Paragraph chain
        # -------------------------------------------------

        parent_node_id = doc_node_id

        for idx, text in enumerate(paragraphs):
            paragraph_sha = self._sha(text)

            paragraph_event_id = await self._create_ingest_event(
                record,
                kind="paragraph",
                url=url,
                suffix=f"paragraph:{idx}:{paragraph_sha}",
            )

            node_id, _ = await add_node_and_edge(
                self.db,
                timeline_id=timeline_id,
                event_id=paragraph_event_id,
                character_id=ACCUMULATOR_ACTOR,
                kind="paragraph",
                speaker_id=ACCUMULATOR_ACTOR,
                speaker_role="system",
                recipient_id=None,
                message_text=text,
                payload={
                    "document_id": str(document_id),
                    "paragraph_index": idx,
                    "paragraph_sha256": paragraph_sha,
                    "source": "internet",
                },
                parent_node_id=parent_node_id,
            )

            parent_node_id = node_id

        logger.info(
            "Ingested document %s (%d paragraphs)",
            document_id,
            len(paragraphs),
        )

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------

    def _split_paragraphs(self, text: str) -> list[str]:
        """
        Split text into semantic paragraphs.

        Fallback:
        - If only one massive blob exists, chunk it defensively.
        """

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        parts = [p.strip() for p in text.split("\n\n") if p.strip()]

        # Defensive fallback for malformed sources
        if len(parts) == 1 and len(parts[0]) > 1500:
            blob = parts[0]
            return [
                blob[i:i + 1200]
                for i in range(0, len(blob), 1200)
            ]

        return parts

    def _sha(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def _create_ingest_event(
        self,
        record: dict,
        *,
        kind: str,
        url: str,
        suffix: str | None = None,
    ) -> int:
        source = f"accumulator:{record['accumulator_id']}"
        base_id = f"{record['raw']['text_sha256']}:{url}"
        source_event_id = f"{base_id}:{suffix}" if suffix else f"{base_id}:{kind}"

        payload = {
            "accumulator_id": record["accumulator_id"],
            "source_url": url,
            "source": "internet",
        }

        return await self.db.create_ingest_event(
            source=source,
            source_event_id=source_event_id,
            kind=kind,
            payload=payload,
            dedupe_key=source_event_id,
        )
