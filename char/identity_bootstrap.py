from __future__ import annotations

import hashlib
import json
from typing import Any

from aios_app.db import Database
from aios_app.char.identity_kernel import IdentityKernelStore


def _card_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _facet_candidates(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Route authored card fields into identity without inventing new facts."""
    candidates: list[dict[str, Any]] = []
    mappings = (
        ("description", "appearance", "description", "constitutional"),
        ("personality", "personality", "description", "core"),
        ("mes_example", "expression", "example_dialogue", "core"),
    )
    for field, facet_type, facet_key, stability in mappings:
        value = _clean(card.get(field))
        if value:
            candidates.append({
                "facet_type": facet_type,
                "facet_key": facet_key,
                "value": value,
                "stability": stability,
                "source_field": field,
                "source_fragment": value,
            })

    # Character-card scenario and first_mes are intentionally excluded: they
    # describe initial circumstances/examples, not durable identity.
    return candidates


async def bootstrap_character_card(
    db: Database,
    *,
    character_id: str,
    payload: dict[str, Any],
    source_name: str | None = None,
    source_format: str = "character_card",
    replace_authored_facets: bool = True,
) -> dict[str, Any]:
    """
    Import an external character card as authored identity source material.

    Phase 1 is deliberately conservative: no LLM extraction and no inference.
    Card prose is preserved verbatim as provenance-backed facets. Scenario and
    first-message fields are not identity and are not imported into the kernel.
    """
    card = _card_data(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    source_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async with db.transaction():
        identity = await db.fetchrow(
            "SELECT character_id, identity_version FROM aios.character_identity WHERE character_id=$1 FOR UPDATE",
            character_id,
        )
        if not identity:
            name = _clean(card.get("name")) or character_id
            await db.execute(
                """
                INSERT INTO aios.character_identity (
                    character_id, canonical_name, display_name, created_from, meta
                )
                VALUES ($1,$2,$2,'character_card',jsonb_build_object('bootstrap','character_card'))
                """,
                character_id, name,
            )

        source = await db.fetchrow(
            """
            INSERT INTO aios.character_identity_source (
                character_id, source_type, source_format, source_name,
                source_hash, raw_payload, authority
            )
            VALUES ($1,'character_card',$2,$3,$4,$5::jsonb,'authored')
            ON CONFLICT (character_id, source_hash) DO UPDATE
            SET source_name=COALESCE(EXCLUDED.source_name, aios.character_identity_source.source_name)
            RETURNING source_id
            """,
            character_id, source_format, source_name, source_hash, raw,
        )
        source_id = source["source_id"]

        name = _clean(card.get("name"))
        if name:
            await db.execute(
                """
                UPDATE aios.character_identity
                SET canonical_name=COALESCE(canonical_name,$2),
                    display_name=COALESCE(display_name,$2),
                    created_from=COALESCE(created_from,'character_card')
                WHERE character_id=$1
                """,
                character_id, name,
            )

        candidates = _facet_candidates(card)
        changed = False
        for item in candidates:
            existing = await db.fetchrow(
                """
                SELECT value, source_id, authority
                FROM aios.character_identity_facet
                WHERE character_id=$1 AND facet_type=$2 AND facet_key=$3
                """,
                character_id, item["facet_type"], item["facet_key"],
            )
            if existing and not replace_authored_facets:
                continue
            value_json = json.dumps(item["value"], ensure_ascii=False)
            if existing and existing["value"] == item["value"] and existing["source_id"] == source_id:
                continue
            await db.execute(
                """
                INSERT INTO aios.character_identity_facet (
                    character_id, facet_type, facet_key, value, stability,
                    authority, mutability, source_id, source_field, source_fragment, status
                )
                VALUES ($1,$2,$3,$4::jsonb,$5,'authored','explicit',$6,$7,$8,'active')
                ON CONFLICT (character_id, facet_type, facet_key) DO UPDATE
                SET value=EXCLUDED.value,
                    stability=EXCLUDED.stability,
                    authority=EXCLUDED.authority,
                    mutability=EXCLUDED.mutability,
                    source_id=EXCLUDED.source_id,
                    source_field=EXCLUDED.source_field,
                    source_fragment=EXCLUDED.source_fragment,
                    status='active',
                    updated_at=now()
                """,
                character_id, item["facet_type"], item["facet_key"], value_json,
                item["stability"], source_id, item["source_field"], item["source_fragment"],
            )
            changed = True

        if changed:
            await db.execute(
                """
                UPDATE aios.character_identity
                SET identity_version=identity_version+1, updated_at=now()
                WHERE character_id=$1
                """,
                character_id,
            )

    kernel = await IdentityKernelStore(db).compile(character_id)
    return {
        "character_id": character_id,
        "source_id": str(source_id),
        "source_hash": source_hash,
        "identity_version": kernel.identity_version,
        "facet_count": len(candidates),
        "kernel_text": kernel.kernel_text,
    }
