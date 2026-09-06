from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

from aios_app.db import Database
from aios_app.dag import add_node_and_edge, get_or_create_timeline

logger = logging.getLogger("accumulator.ingest.jsonl")

LIMINAL_WORLD_KEY = "liminal"
SCOPE_PREFIX = "source:web"
PROVENANCE_VERSION = "provenance-v1"


class JSONLDAGIngestor:
    """
    Project immutable web accumulator records into source_document + liminal DAG.

    Web provenance never becomes character ownership. target_character_id and
    target_world_id remain routing/enrichment hints for downstream logic.
    """

    def __init__(self, db: Database, input_dir: Path):
        self.db = db
        self.input_dir = input_dir

    async def ingest_file(self, path: Path, *, start_at: int = 0) -> int:
        logger.info("Ingesting %s from offset %s", path, start_at)

        with path.open("r", encoding="utf-8") as handle:
            if start_at:
                handle.seek(start_at)

            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                await self._ingest_record(record)

            return handle.tell()

    def _source_context(self, record: dict) -> dict:
        source = dict(record.get("source") or {})
        url = record.get("url") or record.get("requested_url")
        host = (urlparse(url).netloc.lower() if url else "") or "unknown-web-source"

        source_id = source.get("source_id") or host
        source_kind = source.get("source_kind") or "website"
        source_name = source.get("source_name")
        speaker_id = source.get("speaker_id")

        target = dict(record.get("target") or {})
        return {
            "source_id": source_id,
            "source_kind": source_kind,
            "source_name": source_name,
            "speaker_id": speaker_id,
            "target_character_id": target.get("character_id"),
            "target_world_id": target.get("world_id"),
        }

    async def _ensure_source_identity(self, context: dict, url: str) -> None:
        existing = await self.db.fetchrow(
            "SELECT source_kind FROM aios.source_identity WHERE source_id=$1",
            context["source_id"],
        )
        if existing and existing["source_kind"] != context["source_kind"]:
            raise RuntimeError(
                f"web source {context['source_id']!r} is already registered as "
                f"{existing['source_kind']!r}, not {context['source_kind']!r}"
            )

        await self.db.execute(
            """
            INSERT INTO aios.source_identity (
                source_id, source_kind, display_name, canonical_uri,
                canonical_domain, meta
            )
            VALUES ($1,$2,$3,$4,$5,$6::jsonb)
            ON CONFLICT (source_id) DO UPDATE
            SET display_name=COALESCE(aios.source_identity.display_name, EXCLUDED.display_name),
                canonical_uri=COALESCE(aios.source_identity.canonical_uri, EXCLUDED.canonical_uri),
                canonical_domain=COALESCE(aios.source_identity.canonical_domain, EXCLUDED.canonical_domain),
                meta=aios.source_identity.meta || EXCLUDED.meta,
                updated_at=now()
            """,
            context["source_id"],
            context["source_kind"],
            context["source_name"],
            url,
            urlparse(url).netloc.lower() or None,
            json.dumps({"ingestor": "web-jsonl-v2"}),
        )

    async def _source_document(self, record: dict, paragraphs: list[str]) -> UUID:
        url = record["url"]
        content_sha = record.get("raw", {}).get("text_sha256") or self._sha(
            "\n\n".join(paragraphs)
        )
        existing = await self.db.fetchrow(
            """
            SELECT document_id
            FROM aios.source_document
            WHERE source_url=$1
              AND meta->>'content_sha256'=$2
            ORDER BY retrieved_at
            LIMIT 1
            """,
            url,
            content_sha,
        )
        if existing:
            return existing["document_id"]

        document = record.get("document") or {}
        title = document.get("title") or record.get("content", {}).get("title")
        retrieved_at = self._parse_time(record.get("retrieved_at"))

        document_id = uuid4()
        await self.db.execute(
            """
            INSERT INTO aios.source_document (
                document_id, source_type, source_url, title,
                retrieved_at, raw_content, meta
            )
            VALUES ($1,$2,$3,$4,COALESCE($5,now()),$6,$7::jsonb)
            """,
            document_id,
            "internet",
            url,
            title,
            retrieved_at,
            "\n\n".join(paragraphs),
            json.dumps(
                {
                    "content_sha256": content_sha,
                    "html_sha256": record.get("raw", {}).get("html_sha256"),
                    "accumulator_id": record.get("accumulator_id"),
                    "schema_version": record.get("schema_version"),
                    "author": document.get("author"),
                    "published_at": document.get("published_at"),
                    "updated_at": document.get("updated_at"),
                    "canonical_url": document.get("canonical_url"),
                    "site_name": document.get("site_name"),
                    "crawl": record.get("crawl") or {},
                }
            ),
        )
        return document_id

    async def _ingest_record(self, record: dict) -> None:
        url = record["url"]
        content = record.get("content", {}).get("text") or ""
        paragraphs = self._split_paragraphs(content)
        if not paragraphs:
            logger.warning("Empty document: %s", url)
            return

        context = self._source_context(record)
        await self._ensure_source_identity(context, url)
        document_id = await self._source_document(record, paragraphs)

        target_world_id = self._uuid_or_none(context["target_world_id"])
        scope_key = f"{SCOPE_PREFIX}:{context['source_id']}"
        timeline_id = await get_or_create_timeline(
            self.db,
            world_key=LIMINAL_WORLD_KEY,
            session_id=None,
            character_id=None,
            user_name=None,
            scope_key=scope_key,
            meta={
                "source_id": context["source_id"],
                "source_kind": context["source_kind"],
                "source_url": url,
                "world_assignment": "default_liminal",
                "target_character_id": context["target_character_id"],
                "target_world_id": str(target_world_id) if target_world_id else None,
                "provenance_version": PROVENANCE_VERSION,
            },
            source_id=context["source_id"],
        )

        doc_event_id = await self._create_ingest_event(
            record,
            context=context,
            kind="document",
            document_id=document_id,
            url=url,
            target_world_id=target_world_id,
        )
        doc_node_id, _ = await add_node_and_edge(
            self.db,
            timeline_id=timeline_id,
            event_id=doc_event_id,
            character_id=None,
            kind="document",
            speaker_id=context["speaker_id"],
            speaker_role="source",
            recipient_id=None,
            message_text=None,
            payload={
                "document_id": str(document_id),
                "url": url,
                "source_id": context["source_id"],
                "source_kind": context["source_kind"],
                "target_character_id": context["target_character_id"],
                "target_world_id": str(target_world_id) if target_world_id else None,
                "provenance_version": PROVENANCE_VERSION,
            },
            viewpoint_id=None,
        )

        parent_node_id = doc_node_id
        for index, text in enumerate(paragraphs):
            paragraph_sha = self._sha(text)
            paragraph_event_id = await self._create_ingest_event(
                record,
                context=context,
                kind="paragraph",
                document_id=document_id,
                url=url,
                target_world_id=target_world_id,
                suffix=f"paragraph:{index}:{paragraph_sha}",
                message_text=text,
                paragraph_index=index,
            )

            node_id, _ = await add_node_and_edge(
                self.db,
                timeline_id=timeline_id,
                event_id=paragraph_event_id,
                character_id=None,
                kind="paragraph",
                speaker_id=context["speaker_id"],
                speaker_role="source",
                recipient_id=None,
                message_text=text,
                payload={
                    "document_id": str(document_id),
                    "paragraph_index": index,
                    "paragraph_sha256": paragraph_sha,
                    "source_id": context["source_id"],
                    "source_kind": context["source_kind"],
                    "source_url": url,
                    "target_character_id": context["target_character_id"],
                    "target_world_id": str(target_world_id) if target_world_id else None,
                    "provenance_version": PROVENANCE_VERSION,
                },
                viewpoint_id=None,
                parent_node_id=parent_node_id,
            )
            parent_node_id = node_id

        logger.info(
            "Ingested web document %s from source=%s (%d paragraphs)",
            document_id,
            context["source_id"],
            len(paragraphs),
        )

    async def _create_ingest_event(
        self,
        record: dict,
        *,
        context: dict,
        kind: str,
        document_id: UUID,
        url: str,
        target_world_id: UUID | None,
        suffix: str | None = None,
        message_text: str | None = None,
        paragraph_index: int | None = None,
    ) -> int:
        content_sha = record.get("raw", {}).get("text_sha256") or self._sha(
            record.get("content", {}).get("text") or ""
        )
        base_id = f"{content_sha}:{url}"
        source_event_id = f"{base_id}:{suffix}" if suffix else f"{base_id}:{kind}"
        dedupe_key = f"web::{context['source_id']}::{source_event_id}"

        payload = {
            "document_id": str(document_id),
            "source_id": context["source_id"],
            "source_kind": context["source_kind"],
            "source_url": url,
            "accumulator_id": record.get("accumulator_id"),
            "crawl": record.get("crawl") or {},
            "target_character_id": context["target_character_id"],
            "target_world_id": str(target_world_id) if target_world_id else None,
            "provenance_version": PROVENANCE_VERSION,
        }
        if paragraph_index is not None:
            payload["paragraph_index"] = paragraph_index

        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.ingest_event (
                event_time, source, source_id, source_kind, source_event_id,
                kind, session_id, speaker_id, speaker_role, recipient_id,
                viewpoint_id, character_id, user_name, message_text, payload,
                dedupe_key, target_character_id, target_world_id,
                provenance_version
            )
            VALUES (
                COALESCE($1,now()),$2,$3,$4,$5,
                $6::aios.event_kind,NULL,$7,'source'::aios.actor_type,NULL,
                NULL,NULL,NULL,$8,$9::jsonb,
                $10,$11,$12,$13
            )
            ON CONFLICT (dedupe_key) DO UPDATE
            SET dedupe_key=EXCLUDED.dedupe_key
            RETURNING event_id
            """,
            self._parse_time(record.get("retrieved_at")),
            context["source_id"],
            context["source_id"],
            context["source_kind"],
            source_event_id,
            kind,
            context["speaker_id"],
            message_text,
            json.dumps(payload),
            dedupe_key,
            context["target_character_id"],
            target_world_id,
            PROVENANCE_VERSION,
        )
        return int(row["event_id"])

    def _split_paragraphs(self, text: str) -> list[str]:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        parts = [part.strip() for part in text.split("\n\n") if part.strip()]
        if len(parts) == 1 and len(parts[0]) > 1500:
            blob = parts[0]
            return [blob[index:index + 1200] for index in range(0, len(blob), 1200)]
        return parts

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _uuid_or_none(value: str | UUID | None) -> UUID | None:
        if value is None or value == "":
            return None
        if isinstance(value, UUID):
            return value
        return UUID(str(value))
