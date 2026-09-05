from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional
from uuid import UUID, uuid4

from aios_app.db import Database
from aios_app.dag import get_or_create_timeline, add_node_and_edge

LIMINAL_WORLD_KEY = "liminal"
LONGDOC_ACTOR = "document_ingestor"


@dataclass
class Unit:
    unit_type: str
    index: int
    path: str
    title: Optional[str]
    content: str
    depth: int
    start_char: int
    end_char: int


def split_long_document(text: str) -> list[Unit]:
    """Deterministic structure extraction for arbitrary long-form text."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    units: list[Unit] = []
    paragraph_buf: list[str] = []
    char_pos = 0
    paragraph_index = 0
    section_index = 0
    current_section = None

    heading_re = re.compile(
        r"^(?:chapter\s+[\divxlcdm]+\b.*|part\s+[\divxlcdm]+\b.*|"
        r"section\s+\d+\b.*|\d+(?:\.\d+)*\s+[A-Z].*|[A-Z][A-Z0-9 ,:;\-]{4,80})$",
        re.IGNORECASE,
    )

    def flush_paragraph(end_pos: int) -> None:
        nonlocal paragraph_buf, paragraph_index
        content = " ".join(x.strip() for x in paragraph_buf if x.strip()).strip()
        if content:
            path = (
                f"{current_section}/paragraph/{paragraph_index}"
                if current_section
                else f"/paragraph/{paragraph_index}"
            )
            units.append(
                Unit(
                    unit_type="paragraph",
                    index=paragraph_index,
                    path=path,
                    title=None,
                    content=content,
                    depth=1 if current_section else 0,
                    start_char=max(0, end_pos - len(content)),
                    end_char=end_pos,
                )
            )
            paragraph_index += 1
        paragraph_buf = []

    running = 0
    for line in lines:
        stripped = line.strip()
        next_running = running + len(line) + 1
        if stripped and len(stripped) <= 120 and heading_re.match(stripped):
            flush_paragraph(running)
            current_section = f"/section/{section_index}"
            units.append(
                Unit(
                    unit_type="section",
                    index=section_index,
                    path=current_section,
                    title=stripped,
                    content="",
                    depth=0,
                    start_char=running,
                    end_char=next_running,
                )
            )
            section_index += 1
        elif not stripped:
            flush_paragraph(running)
        else:
            paragraph_buf.append(stripped)
        running = next_running

    flush_paragraph(len(text))
    return units


def derive_metadata(text: str, *, supplied_title: Optional[str] = None) -> list[dict]:
    """Extract only metadata actually observable in the document itself."""
    observations: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    if supplied_title:
        observations.append({
            "field_type": "title",
            "raw_value": supplied_title,
            "normalized_value": supplied_title.strip(),
            "source_location": "ingest_context",
            "confidence": 0.95,
            "extraction_method": "provided_with_document",
        })
    elif lines:
        candidate = lines[0]
        if 2 <= len(candidate.split()) <= 20 and len(candidate) <= 180:
            observations.append({
                "field_type": "title",
                "raw_value": candidate,
                "normalized_value": candidate,
                "source_location": "document_start",
                "confidence": 0.65,
                "extraction_method": "document_first_line",
            })

    patterns = [
        ("author", re.compile(r"^(?:by|author[:\s]+)\s*(.{2,120})$", re.I), 0.75),
        ("isbn", re.compile(r"\bISBN(?:-1[03])?\s*:?\s*([0-9Xx\- ]{10,25})\b", re.I), 0.9),
        ("doi", re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b", re.I), 0.9),
        ("publication_year", re.compile(r"(?:copyright|©|published)\D{0,20}((?:18|19|20)\d{2})", re.I), 0.7),
        ("publisher", re.compile(r"^(?:publisher[:\s]+|published by\s+)(.{2,160})$", re.I), 0.7),
        ("edition", re.compile(r"\b(\d+(?:st|nd|rd|th)\s+edition|revised edition|second edition|third edition)\b", re.I), 0.7),
    ]

    seen: set[tuple[str, str]] = set()
    for idx, line in enumerate(lines):
        for field_type, pattern, confidence in patterns:
            match = pattern.search(line)
            if not match:
                continue
            raw = (match.group(1) if match.groups() else match.group(0)).strip()
            key = (field_type, raw.lower())
            if key in seen:
                continue
            seen.add(key)
            observations.append({
                "field_type": field_type,
                "raw_value": raw,
                "normalized_value": re.sub(r"\s+", " ", raw),
                "source_location": f"line:{idx}",
                "confidence": confidence,
                "extraction_method": "document_regex_v1",
            })
    return observations


async def ingest_long_document(
    db: Database,
    *,
    text: str,
    source_type: str = "document",
    source_uri: Optional[str] = None,
    title: Optional[str] = None,
    source_name: str = "long_document",
) -> dict:
    if not text.strip():
        raise ValueError("document text is empty")

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = await db.fetchrow(
        """
        SELECT document_id
        FROM aios.source_document
        WHERE meta->>'content_sha256'=$1
        ORDER BY retrieved_at
        LIMIT 1
        """,
        content_hash,
    )
    if existing:
        return {"document_id": existing["document_id"], "deduplicated": True}

    document_id = uuid4()
    await db.execute(
        """
        INSERT INTO aios.source_document (
            document_id, source_type, source_url, title, raw_content, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6::jsonb)
        """,
        document_id,
        source_type,
        source_uri,
        title,
        text,
        json.dumps({"content_sha256": content_hash, "ingestor": "longdoc-v1"}),
    )

    timeline_id = await get_or_create_timeline(
        db,
        world_key=LIMINAL_WORLD_KEY,
        session_id=None,
        character_id=LONGDOC_ACTOR,
        user_name=LONGDOC_ACTOR,
        scope_key=f"source:document:{document_id}",
        meta={
            "source": source_name,
            "source_uri": source_uri,
            "document_id": str(document_id),
        },
    )

    root_event = await db.create_ingest_event(
        source=source_name,
        source_event_id=f"{content_hash}:document",
        kind="document",
        payload={
            "document_id": str(document_id),
            "source_uri": source_uri,
            "content_sha256": content_hash,
        },
        dedupe_key=f"document::{content_hash}",
    )
    root_node, _ = await add_node_and_edge(
        db,
        timeline_id=timeline_id,
        event_id=root_event,
        character_id=LONGDOC_ACTOR,
        kind="document",
        speaker_id=LONGDOC_ACTOR,
        speaker_role="system",
        recipient_id=None,
        message_text=None,
        payload={"document_id": str(document_id), "source_uri": source_uri},
    )

    root_unit = await db.execute_returning_row(
        """
        INSERT INTO aios.document_unit (
            document_id, node_id, unit_type, unit_index, path,
            title, content, depth, start_char, end_char, meta
        )
        VALUES ($1,$2,'document',0,'/',$3,NULL,0,0,$4,$5::jsonb)
        RETURNING unit_id
        """,
        document_id,
        root_node,
        title,
        len(text),
        json.dumps({"content_sha256": content_hash}),
    )

    units = split_long_document(text)
    parent_section_unit: Optional[UUID] = None
    parent_dag_node = root_node
    paragraph_nodes = 0

    for unit in units:
        if unit.unit_type == "section":
            rec = await db.execute_returning_row(
                """
                INSERT INTO aios.document_unit (
                    document_id, parent_unit_id, unit_type, unit_index, path,
                    title, content, depth, start_char, end_char, meta
                )
                VALUES ($1,$2,'section',$3,$4,$5,NULL,$6,$7,$8,'{}'::jsonb)
                RETURNING unit_id
                """,
                document_id,
                root_unit["unit_id"],
                unit.index,
                unit.path,
                unit.title,
                unit.depth,
                unit.start_char,
                unit.end_char,
            )
            parent_section_unit = rec["unit_id"]
            continue

        event_id = await db.create_ingest_event(
            source=source_name,
            source_event_id=f"{content_hash}:paragraph:{unit.index}",
            kind="paragraph",
            payload={
                "document_id": str(document_id),
                "paragraph_index": unit.index,
                "document_path": unit.path,
            },
            dedupe_key=f"document::{content_hash}::paragraph::{unit.index}",
        )
        node_id, _ = await add_node_and_edge(
            db,
            timeline_id=timeline_id,
            event_id=event_id,
            character_id=LONGDOC_ACTOR,
            kind="paragraph",
            speaker_id=LONGDOC_ACTOR,
            speaker_role="system",
            recipient_id=None,
            message_text=unit.content,
            payload={
                "document_id": str(document_id),
                "paragraph_index": unit.index,
                "document_path": unit.path,
            },
            parent_node_id=parent_dag_node,
        )
        parent_dag_node = node_id
        paragraph_nodes += 1

        await db.execute(
            """
            INSERT INTO aios.document_unit (
                document_id, parent_unit_id, node_id, unit_type, unit_index,
                path, title, content, depth, start_char, end_char, meta
            )
            VALUES ($1,$2,$3,'paragraph',$4,$5,NULL,$6,$7,$8,$9,'{}'::jsonb)
            """,
            document_id,
            parent_section_unit or root_unit["unit_id"],
            node_id,
            unit.index,
            unit.path,
            unit.content,
            unit.depth,
            unit.start_char,
            unit.end_char,
        )

    for md in derive_metadata(text, supplied_title=title):
        await db.execute(
            """
            INSERT INTO aios.document_metadata_observation (
                document_id, field_type, raw_value, normalized_value,
                source_location, confidence, extraction_method, meta
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,'{}'::jsonb)
            """,
            document_id,
            md["field_type"],
            md["raw_value"],
            md["normalized_value"],
            md["source_location"],
            md["confidence"],
            md["extraction_method"],
        )

    return {
        "document_id": document_id,
        "timeline_id": timeline_id,
        "root_node_id": root_node,
        "paragraph_nodes": paragraph_nodes,
        "unit_count": len(units) + 1,
        "deduplicated": False,
    }
