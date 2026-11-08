"""SillyTavern World Info / character_book -> cold AIOS corpus adapter.

No chat, DAG, cognition, or runtime lorebook activation occurs here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aios_app.corpus import import_corpus_document
from aios_app.corpus_routing import CorpusFacetRouter
from aios_app.db import Database


MAX_BYTES = 12 * 1024 * 1024
MAX_ENTRIES = 10000


def parse_lorebook(path: Path, *, include_disabled: bool = False) -> dict[str, Any]:
    if path.suffix.lower() != ".json":
        raise ValueError("Lorebook must be a .json file")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Lorebook exceeds the 12 MiB import limit")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid lorebook JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Lorebook root must be an object")

    book = payload
    format_name = "world_info"
    if isinstance(payload.get("character_book"), dict):
        book = payload["character_book"]
        format_name = "character_book"
    elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("character_book"), dict):
        book = payload["data"]["character_book"]
        format_name = "character_book"
    elif isinstance(payload.get("entries"), (dict, list)):
        format_name = "world_info" if isinstance(payload["entries"], dict) else "character_book"
    elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("entries"), (dict, list)):
        book = payload["data"]
        format_name = "world_info" if isinstance(book["entries"], dict) else "character_book"
    else:
        raise ValueError("No SillyTavern entries or character_book found")

    entries = book.get("entries")
    if not isinstance(entries, (dict, list)):
        raise ValueError("Lorebook entries must be an object or array")
    pairs = list(entries.items()) if isinstance(entries, dict) else list(enumerate(entries))
    if len(pairs) > MAX_ENTRIES:
        raise ValueError("Lorebook exceeds the 10,000-entry import limit")

    normalized = []
    disabled_count = 0
    for index, (entry_key, value) in enumerate(pairs):
        if not isinstance(value, dict):
            raise ValueError(f"Entry {entry_key!r} is not an object")
        enabled = bool(value.get("enabled", not bool(value.get("disable", False))))
        if not enabled:
            disabled_count += 1
            if not include_disabled:
                continue
        body = value.get("content", "")
        if not isinstance(body, str):
            raise ValueError(f"Entry {entry_key!r} has non-text content")
        if not body.strip():
            continue
        heading = str(value.get("comment") or value.get("name") or value.get("title") or f"Entry {entry_key}").strip()
        # Retain foreign activation attributes as provenance only. They NEVER
        # control AIOS retrieval ranking, ACLs, or prompt insertion.
        meta = {
            "source_format": format_name,
            "entry_key": str(entry_key),
            "source_uid": value.get("uid", value.get("id", entry_key)),
            "enabled": enabled,
            "keys": value.get("key", value.get("keys", [])),
            "secondary_keys": value.get("keysecondary", value.get("secondary_keys", [])),
            "sillytavern": {k: v for k, v in value.items() if k != "content"},
        }
        normalized.append({
            "heading": heading[:500],
            "path": f"entries/{index}",
            "content": body.strip(),
            "meta": meta,
        })
    if not normalized:
        raise ValueError("Lorebook has no importable nonempty entries")
    title = str(book.get("name") or payload.get("name") or path.stem).strip() or path.stem
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "title": title,
        "format": format_name,
        "sha256": digest,
        "file_name": path.name,
        "total_entries": len(pairs),
        "disabled_entries": disabled_count,
        "sections": normalized,
    }


async def import_lorebook(
    db: Database, path: Path, domain_key: str, *,
    include_disabled: bool = False,
) -> dict[str, Any]:
    parsed = parse_lorebook(path, include_disabled=include_disabled)
    router = CorpusFacetRouter(db)
    # Explicit operator choice, never universe inference from title/prose.
    route = await router.resolve_domain_key(
        domain_key, evidence="operator_lorebook_import"
    )
    # File hash identifies one exact source revision. Same file can be assigned
    # to multiple registered domains without creating a duplicate document.
    source_id = f"sillytavern-lorebook:{parsed['sha256']}"
    text = "\n\n".join(
        f"## {section['heading']}\n{section['content']}"
        for section in parsed["sections"]
    )
    imported = await import_corpus_document(
        db, text=text, source_id=source_id,
        source_kind="sillytavern_lorebook",
        title=parsed["title"], source_uri=f"sillytavern-lorebook://{parsed['sha256']}",
        meta={
            "source_format": parsed["format"],
            "source_file": parsed["file_name"],
            "source_sha256": parsed["sha256"],
            "total_entries": parsed["total_entries"],
            "disabled_entries": parsed["disabled_entries"],
            "include_disabled": include_disabled,
        },
        scopes=route.scopes,
        epistemic_namespace=route.epistemic_namespace,
        identity_binding="external",
        authored_sections=parsed["sections"],
    )
    await router.apply(document_id=imported["document_id"], route=route)
    return {
        "title": parsed["title"],
        "domain": domain_key,
        "document_id": str(imported["document_id"]),
        "section_count": imported["section_count"],
        "total_entries": parsed["total_entries"],
        "disabled_entries": parsed["disabled_entries"],
        "deduplicated": imported["deduplicated"],
    }
