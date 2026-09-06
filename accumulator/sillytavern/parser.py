from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ParsedMessage:
    index: int
    speaker_id: str
    speaker_type: str
    viewpoint_id: str | None
    text: str
    event_time: datetime | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ParsedChatLog:
    path: Path
    file_sha256: str
    chat_key: str
    user_name: str
    character_name: str
    create_date: str | None
    chat_metadata: dict[str, Any]
    messages: list[ParsedMessage]


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_time(value: str | None) -> datetime | None:
    """
    Parse only timestamps that carry an explicit timezone.

    SillyTavern send_date strings such as "May 17, 2025 8:51pm" are local
    wall-clock values with no offset. Inventing UTC would corrupt chronology,
    so those remain raw provenance and DAG event order remains authoritative.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None

