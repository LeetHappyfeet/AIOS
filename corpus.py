from __future__ import annotations

import hashlib
import json
from typing import Iterable
from uuid import UUID

from aios_app.db import Database
from aios_app.documents.long_document import split_long_document
from aios_app.external_observation import ensure_source_identity, persist_external_observation
from aios_app.models import ExternalObservationIn


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def import_corpus_document(
    db: Database,
    *,
    text: str,
    source_id: str,
    source_kind: str = "document",
    title: str | None = None,
    author: str | None = None,
    source_uri: str | None = None,
    language: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Store a document as cold searchable material with no semantic side effects."""
    if not text.strip():
        raise ValueError("corpus document text is empty")

    source_req = ExternalObservationIn(
        source_id=source_id,
        source_kind=source_kind,
        source_name=title or source_id,
        source_uri=source_uri,
        source_meta={"corpus": True},
        text="corpus source registration",
    )
    await ensure_source_identity(db, source_req)

    content_hash = _sha256(text)
    existing = await db.fetchrow(
        "SELECT document_id FROM aios.corpus_document WHERE content_hash=$1",
        content_hash,
    )
    if existing:
        count = await db.fetchrow(
            "SELECT count(*) AS n FROM aios.corpus_section WHERE document_id=$1",
            existing["document_id"],
        )
        return {
            "document_id": existing["document_id"],
            "section_count": int(count["n"]),
            "deduplicated": True,
        }

    document = await db.execute_returning_row(
        """
        INSERT INTO aios.corpus_document (
            source_id, document_kind, title, author, source_uri, language,
            content_hash, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
        RETURNING document_id
        """,
        source_id, source_kind, title, author, source_uri, language,
        content_hash, json.dumps(meta or {}),
    )
    document_id = document["document_id"]

    units = split_long_document(text)
    heading: str | None = None
    section_order = 0
    for unit in units:
        if unit.unit_type == "section":
            heading = unit.title
            continue
        if not unit.content.strip():
            continue
        await db.execute(
            """
            INSERT INTO aios.corpus_section (
                document_id, section_order, section_path, heading,
                content, content_hash, meta
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
            """,
            document_id,
            section_order,
            unit.path,
            heading,
            unit.content,
            _sha256(unit.content),
            json.dumps({"start_char": unit.start_char, "end_char": unit.end_char}),
        )
        section_order += 1

    return {
        "document_id": document_id,
        "section_count": section_order,
        "deduplicated": False,
    }


async def consume_corpus_sections(
    db: Database,
    *,
    instance_id: UUID,
    section_ids: Iterable[UUID],
    mode: str = "read",
) -> dict:
    """Cross selected cold sections into normal ingestion for one character only."""
    if mode not in {"read", "research", "taught", "import"}:
        raise ValueError(f"unsupported consumption mode {mode!r}")

    instance = await db.fetchrow(
        "SELECT character_id FROM aios.character_instance WHERE instance_id=$1",
        instance_id,
    )
    if not instance:
        raise ValueError(f"unknown character instance {instance_id}")

    consumed: list[UUID] = []
    for section_id in section_ids:
        row = await db.fetchrow(
            """
            SELECT cs.section_id, cs.document_id, cs.content, cs.heading,
                   cd.source_id, cd.document_kind, cd.title, cd.source_uri
            FROM aios.corpus_section cs
            JOIN aios.corpus_document cd ON cd.document_id=cs.document_id
            WHERE cs.section_id=$1
            """,
            section_id,
        )
        if not row:
            raise ValueError(f"unknown corpus section {section_id}")

        receipt = await db.execute_returning_row(
            """
            INSERT INTO aios.source_consumption (
                instance_id, document_id, section_id, mode, status, meta
            )
            VALUES ($1,$2,$3,$4,'pending',$5::jsonb)
            ON CONFLICT (instance_id, section_id, mode) DO UPDATE
            SET meta=aios.source_consumption.meta || EXCLUDED.meta
            RETURNING consumption_id, ingest_event_id, status
            """,
            instance_id, row["document_id"], section_id, mode,
            json.dumps({"title": row["title"], "heading": row["heading"]}),
        )
        if receipt["ingest_event_id"] is not None and receipt["status"] == "ingested":
            consumed.append(receipt["consumption_id"])
            continue

        try:
            result = await persist_external_observation(
                db,
                ExternalObservationIn(
                    source_id=row["source_id"] or f"corpus:{row['document_id']}",
                    source_kind=row["document_kind"] or "document",
                    source_name=row["title"],
                    source_uri=row["source_uri"],
                    source_event_id=f"consume:{receipt['consumption_id']}",
                    target_character_id=instance["character_id"],
                    text=row["content"],
                    payload={
                        "corpus_document_id": str(row["document_id"]),
                        "corpus_section_id": str(section_id),
                        "consumption_id": str(receipt["consumption_id"]),
                        "acquisition_mode": mode,
                        "target_instance_id": str(instance_id),
                        "intentional_consumption": True,
                    },
                    dedupe_key=f"corpus-consume::{receipt['consumption_id']}",
                    scope_key=f"corpus:{row['document_id']}:instance:{instance_id}",
                ),
            )
            await db.execute(
                """
                UPDATE aios.source_consumption
                SET status='ingested', ingest_event_id=$2, consumed_at=now(), error=NULL
                WHERE consumption_id=$1
                """,
                receipt["consumption_id"], result.event_id,
            )
            consumed.append(receipt["consumption_id"])
        except Exception as exc:
            await db.execute(
                "UPDATE aios.source_consumption SET status='failed', error=$2 WHERE consumption_id=$1",
                receipt["consumption_id"], str(exc),
            )
            raise

    return {"instance_id": instance_id, "mode": mode, "consumption_ids": consumed}
