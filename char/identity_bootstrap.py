from __future__ import annotations

import hashlib
import json
from typing import Any

from aios_app.db import Database
from aios_app.char.identity_kernel import IdentityKernelStore
from aios_app.char.identity_revision import accept_identity_candidate
from aios_app.char.domain_resolution import CharacterDomainResolver


def _card_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None



def _authored_domain_declarations(card: dict[str, Any]) -> list[dict[str, str]]:
    """Read only explicit AIOS domain metadata; never infer domains from prose."""
    extensions = card.get("extensions")
    extension_aios = extensions.get("aios") if isinstance(extensions, dict) else None
    aios = card.get("aios") if isinstance(card.get("aios"), dict) else extension_aios
    if not isinstance(aios, dict):
        return []
    identity = aios.get("identity")
    if not isinstance(identity, dict):
        return []
    raw_domains = identity.get("domains")
    if not isinstance(raw_domains, list):
        return []

    declarations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_domains:
        if not isinstance(item, dict):
            continue
        domain = _clean(item.get("domain"))
        relationship = (_clean(item.get("relationship")) or "native").lower()
        if not domain or relationship not in {"native", "crossover"}:
            continue
        key = (domain, relationship)
        if key in seen:
            continue
        seen.add(key)
        declarations.append({"domain": domain, "relationship": relationship})
    return declarations


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

    for declaration in _authored_domain_declarations(card):
        domain = declaration["domain"]
        relationship = declaration["relationship"]
        candidates.append({
            "facet_type": "domain",
            "facet_key": domain,
            "value": {"domain": domain, "relationship": relationship},
            "stability": "structural",
            "source_field": "aios.identity.domains",
            "source_fragment": f"{domain}:{relationship}",
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
    auto_accept_authored: bool = True,
) -> dict[str, Any]:
    """
    Import an external character card as authored identity source material.

    Import is deliberately conservative: no LLM extraction and no inference.
    Card prose becomes provenance-backed candidates first. Authored cards may
    auto-accept those candidates; other source types can use the same staging
    boundary without silently rewriting identity. Scenario and first-message
    fields are not identity and are not imported into the kernel.
    """
    card = _card_data(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    source_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async with db.connection() as con:
      async with con.transaction():
        identity = await con.fetchrow(
            "SELECT character_id, identity_version FROM aios.character_identity WHERE character_id=$1 FOR UPDATE",
            character_id,
        )
        if not identity:
            name = _clean(card.get("name")) or character_id
            await con.execute(
                """
                INSERT INTO aios.character_identity (
                    character_id, canonical_name, display_name, created_from, meta
                )
                VALUES ($1,$2,$2,'character_card',jsonb_build_object('bootstrap','character_card'))
                """,
                character_id, name,
            )

        source = await con.fetchrow(
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
            await con.execute(
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

        # Source-native structured franchise/fandom/universe metadata may resolve
        # through the trusted domain registry. This never examines description,
        # personality, scenario, dialogue, or other prose.
        resolved_domains = await CharacterDomainResolver(db).resolve(
            character_id=character_id,
            source_id=source_id,
            card=card,
        )
        explicit_domains = {
            item["facet_key"]
            for item in candidates
            if item.get("facet_type") == "domain"
        }
        for declaration in resolved_domains:
            domain = declaration["domain"]
            if domain in explicit_domains:
                continue
            candidates.append({
                "facet_type": "domain",
                "facet_key": domain,
                "value": {
                    "domain": domain,
                    "relationship": declaration["relationship"],
                },
                "stability": "structural",
                "source_field": declaration["source_field"],
                "source_fragment": declaration["source_fragment"],
                "meta": declaration.get("meta") or {},
            })

        candidate_ids: list[str] = []
        for item in candidates:
            row = await con.fetchrow(
                """
                INSERT INTO aios.character_identity_candidate (
                    character_id, source_id, facet_type, facet_key, value,
                    stability, authority, mutability, perspective,
                    source_field, source_fragment, disposition, meta
                )
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,'authored','explicit','self',$7,$8,'proposed',$9::jsonb)
                ON CONFLICT (source_id, facet_type, facet_key, source_field) DO UPDATE
                SET value=EXCLUDED.value,
                    stability=EXCLUDED.stability,
                    source_fragment=EXCLUDED.source_fragment
                RETURNING candidate_id, disposition
                """,
                character_id, source_id, item["facet_type"], item["facet_key"],
                json.dumps(item["value"], ensure_ascii=False), item["stability"],
                item["source_field"], item["source_fragment"],
                json.dumps(item.get("meta") or {}),
            )
            candidate_ids.append(str(row["candidate_id"]))

    accepted = []
    if auto_accept_authored:
        for candidate_id in candidate_ids:
            if not replace_authored_facets:
                candidate = await db.fetchrow(
                    """
                    SELECT facet_type, facet_key
                    FROM aios.character_identity_candidate
                    WHERE candidate_id=$1::uuid
                    """,
                    candidate_id,
                )
                existing = await db.fetchrow(
                    """
                    SELECT 1
                    FROM aios.character_identity_facet
                    WHERE character_id=$1 AND facet_type=$2 AND facet_key=$3 AND status='active'
                    """,
                    character_id, candidate["facet_type"], candidate["facet_key"],
                )
                if existing:
                    continue
            result = await accept_identity_candidate(
                db,
                candidate_id,
                actor="character_card_bootstrap",
                reason="authored character card import",
            )
            if result.get("changed"):
                accepted.append(candidate_id)

    kernel = await IdentityKernelStore(db).compile(character_id)
    return {
        "character_id": character_id,
        "source_id": str(source_id),
        "source_hash": source_hash,
        "identity_version": kernel.identity_version,
        "candidate_count": len(candidate_ids),
        "accepted_count": len(accepted),
        "candidate_ids": candidate_ids,
        "kernel_text": kernel.kernel_text,
    }
