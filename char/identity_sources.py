from __future__ import annotations

import hashlib
import json
from typing import Any

from aios_app.db import Database
from aios_app.char.identity_kernel import _json_value


ALLOWED_SOURCE_TYPES = {
    "character_card",
    "manual",
    "wiki",
    "biography",
    "canonical_reference",
    "developmental",
}
ALLOWED_PERSPECTIVES = {
    "self",
    "biographical",
    "public_reputation",
    "secret",
    "unknown",
}
ALLOWED_STABILITIES = {"structural", "constitutional", "core", "developmental"}
ALLOWED_MUTABILITIES = {"locked", "explicit", "developmental"}


def _stable_payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def stage_identity_source(
    db: Database,
    *,
    character_id: str,
    source_type: str,
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
    source_name: str | None = None,
    source_format: str | None = None,
    authority: str = "reference",
    continuity_key: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stage externally interpreted identity material without changing identity.

    This is the generic Phase-2/3 boundary. A card parser, wiki adapter, human,
    or future extractor may propose candidates here. Staging never grants those
    proposals write authority over the durable kernel.
    """
    if source_type not in ALLOWED_SOURCE_TYPES:
        raise ValueError(f"unsupported identity source_type {source_type!r}")

    source_hash = _stable_payload_hash(payload)
    raw = json.dumps(payload, ensure_ascii=False)
    candidate_ids: list[str] = []

    async with db.connection() as con:
        async with con.transaction():
            identity = await con.fetchrow(
                "SELECT character_id FROM aios.character_identity WHERE character_id=$1 FOR UPDATE",
                character_id,
            )
            if not identity:
                raise LookupError(
                    f"character {character_id!r} must be structurally registered before non-card identity staging"
                )

            source = await con.fetchrow(
                """
                INSERT INTO aios.character_identity_source (
                    character_id, source_type, source_format, source_name,
                    source_hash, raw_payload, authority, meta
                )
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8::jsonb)
                ON CONFLICT (character_id, source_hash) DO UPDATE
                SET source_name=COALESCE(EXCLUDED.source_name, aios.character_identity_source.source_name),
                    meta=aios.character_identity_source.meta || EXCLUDED.meta
                RETURNING source_id
                """,
                character_id, source_type, source_format, source_name,
                source_hash, raw, authority, json.dumps(meta or {}),
            )
            source_id = source["source_id"]

            for index, item in enumerate(candidates):
                facet_type = str(item.get("facet_type") or "").strip()
                facet_key = str(item.get("facet_key") or "").strip()
                if not facet_type or not facet_key:
                    raise ValueError(f"candidate {index} requires facet_type and facet_key")
                stability = str(item.get("stability") or "core")
                mutability = str(item.get("mutability") or "explicit")
                perspective = str(item.get("perspective") or "unknown")
                if stability not in ALLOWED_STABILITIES:
                    raise ValueError(f"candidate {index} has invalid stability {stability!r}")
                if mutability not in ALLOWED_MUTABILITIES:
                    raise ValueError(f"candidate {index} has invalid mutability {mutability!r}")
                if perspective not in ALLOWED_PERSPECTIVES:
                    raise ValueError(f"candidate {index} has invalid perspective {perspective!r}")

                row = await con.fetchrow(
                    """
                    INSERT INTO aios.character_identity_candidate (
                        character_id, source_id, facet_type, facet_key, value,
                        stability, authority, mutability, perspective,
                        continuity_key, source_field, source_fragment,
                        disposition, meta
                    )
                    VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,'proposed',$13::jsonb)
                    ON CONFLICT (source_id, facet_type, facet_key, source_field) DO UPDATE
                    SET value=EXCLUDED.value,
                        stability=EXCLUDED.stability,
                        authority=EXCLUDED.authority,
                        mutability=EXCLUDED.mutability,
                        perspective=EXCLUDED.perspective,
                        continuity_key=EXCLUDED.continuity_key,
                        source_fragment=EXCLUDED.source_fragment,
                        meta=aios.character_identity_candidate.meta || EXCLUDED.meta
                    RETURNING candidate_id
                    """,
                    character_id, source_id, facet_type, facet_key,
                    json.dumps(item.get("value"), ensure_ascii=False),
                    stability, str(item.get("authority") or authority), mutability,
                    perspective, item.get("continuity_key", continuity_key),
                    item.get("source_field") or "", item.get("source_fragment"),
                    json.dumps(item.get("meta") or {}),
                )
                candidate_ids.append(str(row["candidate_id"]))

    return {
        "character_id": character_id,
        "source_id": str(source_id),
        "source_hash": source_hash,
        "candidate_ids": candidate_ids,
        "candidate_count": len(candidate_ids),
        "disposition": "proposed",
    }


async def identity_snapshot(db: Database, character_id: str) -> dict[str, Any]:
    identity = await db.fetchrow(
        "SELECT * FROM aios.character_identity WHERE character_id=$1",
        character_id,
    )
    if not identity:
        raise LookupError(f"unknown character {character_id}")
    sources = await db.fetch(
        """
        SELECT source_id, source_type, source_format, source_name, source_hash,
               authority, imported_at, meta
        FROM aios.character_identity_source
        WHERE character_id=$1
        ORDER BY imported_at, source_id
        """,
        character_id,
    )
    facets = await db.fetch(
        """
        SELECT facet_id, facet_type, facet_key, value, stability, authority,
               mutability, source_id, source_field, source_fragment, status,
               created_at, updated_at, meta
        FROM aios.character_identity_facet
        WHERE character_id=$1
        ORDER BY facet_type, facet_key
        """,
        character_id,
    )
    candidates = await db.fetch(
        """
        SELECT candidate_id, source_id, facet_type, facet_key, value, stability,
               authority, mutability, perspective, continuity_key, source_field,
               source_fragment, disposition, accepted_facet_id, created_at,
               decided_at, meta
        FROM aios.character_identity_candidate
        WHERE character_id=$1
        ORDER BY created_at, candidate_id
        """,
        character_id,
    )
    revisions = await db.fetch(
        """
        SELECT revision_id, identity_version, operation, source_id, candidate_id,
               facet_id, previous_value, new_value, actor, reason, created_at, meta
        FROM aios.character_identity_revision
        WHERE character_id=$1
        ORDER BY identity_version
        """,
        character_id,
    )
    def normalized(row: Any, json_fields: set[str]) -> dict[str, Any]:
        item = dict(row)
        for field in json_fields:
            if field in item:
                item[field] = _json_value(item[field])
        return item

    return {
        "identity": normalized(identity, {"meta"}),
        "sources": [normalized(row, {"meta"}) for row in sources],
        "facets": [normalized(row, {"value", "meta"}) for row in facets],
        "candidates": [normalized(row, {"value", "meta"}) for row in candidates],
        "revisions": [
            normalized(row, {"previous_value", "new_value", "meta"})
            for row in revisions
        ],
    }
