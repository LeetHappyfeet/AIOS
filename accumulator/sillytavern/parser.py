from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any


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
    Parse only timestamps carrying an explicit timezone.

    SillyTavern send_date strings such as "May 17, 2025 8:51pm" are local
    wall-clock values without an offset. They remain raw provenance instead
    of being assigned a fabricated timezone.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _speaker_type(message: dict[str, Any]) -> str:
    if bool(message.get("is_system")):
        return "system"
    if bool(message.get("is_user")):
        return "user"
    return "character"


def _viewpoint_id(
    message: dict[str, Any],
    speaker_type: str,
    fallback_speaker: str,
) -> str | None:
    speaker_id = str(message.get("name") or "").strip() or fallback_speaker
    if speaker_type in {"user", "character"}:
        return speaker_id
    return None


def parse_sillytavern_jsonl(path: Path) -> ParsedChatLog:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Line {line_number} is not a JSON object")
            rows.append(row)

    if not rows:
        raise ValueError("Chat log is empty")

    header = rows[0]
    user_name = str(header.get("user_name") or "").strip()
    character_name = str(header.get("character_name") or "").strip()
    if not user_name or not character_name:
        raise ValueError(
            "SillyTavern header must contain user_name and character_name"
        )

    chat_metadata = header.get("chat_metadata") or {}
    if not isinstance(chat_metadata, dict):
        chat_metadata = {}

    file_sha = _file_sha(path)
    integrity = str(chat_metadata.get("integrity") or "").strip()
    chat_hash = str(chat_metadata.get("chat_id_hash") or "").strip()
    chat_key = integrity or chat_hash or file_sha

    messages: list[ParsedMessage] = []
    for index, raw in enumerate(rows[1:]):
        if "mes" not in raw:
            continue

        text = str(raw.get("mes") or "").strip()
        if not text:
            continue

        speaker_type = _speaker_type(raw)
        if speaker_type == "user":
            fallback_speaker = user_name
        elif speaker_type == "character":
            fallback_speaker = character_name
        else:
            fallback_speaker = "system"

        speaker_id = str(raw.get("name") or "").strip() or fallback_speaker

        messages.append(
            ParsedMessage(
                index=index,
                speaker_id=speaker_id,
                speaker_type=speaker_type,
                viewpoint_id=_viewpoint_id(
                    raw,
                    speaker_type,
                    fallback_speaker,
                ),
                text=text,
                event_time=(
                    _parse_time(raw.get("gen_started"))
                    or _parse_time(raw.get("send_date"))
                ),
                raw=raw,
            )
        )

    if not messages:
        raise ValueError(
            "No SillyTavern message records containing 'mes' were found"
        )

    return ParsedChatLog(
        path=path,
        file_sha256=file_sha,
        chat_key=chat_key,
        user_name=user_name,
        character_name=character_name,
        create_date=header.get("create_date"),
        chat_metadata=chat_metadata,
        messages=messages,
    )


def summarize_chat_log(path: Path) -> dict[str, Any]:
    parsed = parse_sillytavern_jsonl(path)
    user_messages = sum(
        1 for item in parsed.messages if item.speaker_type == "user"
    )
    character_messages = sum(
        1 for item in parsed.messages if item.speaker_type == "character"
    )
    system_messages = sum(
        1 for item in parsed.messages if item.speaker_type == "system"
    )
    return {
        "file": path.name,
        "chat_key": parsed.chat_key,
        "user_name": parsed.user_name,
        "character_name": parsed.character_name,
        "messages": len(parsed.messages),
        "user_messages": user_messages,
        "character_messages": character_messages,
        "system_messages": system_messages,
        "file_sha256": parsed.file_sha256,
    }
