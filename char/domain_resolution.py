from __future__ import annotations

import json
import re
from typing import Any, Mapping

from aios_app.db import Database
from aios_app.corpus_routing import normalize_facet_value


TRUSTED_IDENTIFIER_FIELDS = {
    "fandom": "fandom",
    "fandoms": "fandom",
    "franchise": "franchise",
    "franchises": "franchise",
    "universe": "universe",
    "universes": "universe",
    "subject": "subject",
    "subjects": "subject",
    "domain": "domain",
    "domains": "domain",
}
RELATIONSHIPS = {"native", "crossover"}


def _values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
            elif isinstance(item, Mapping):
                raw = item.get("name") or item.get("value") or item.get("identifier")
                if isinstance(raw, str) and raw.strip():
                    result.append(raw.strip())
        return tuple(result)
    return ()


def structured_character_identifiers(card: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Extract only explicit structured affiliation fields; never inspect prose."""
    containers: list[tuple[str, Mapping[str, Any]]] = []
    for key in ("metadata", "meta", "catalog", "source_metadata"):
        value = card.get(key)
        if isinstance(value, Mapping):
            containers.append((key, value))

    extensions = card.get("extensions")
    if isinstance(extensions, Mapping):
        for provider, value in extensions.items():
            if provider == "aios":
                continue
            if isinstance(value, Mapping):
                containers.append((f"extensions.{provider}", value))

    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for prefix, container in containers:
        relationship = str(container.get("relationship") or "native").strip().lower()
        if relationship not in RELATIONSHIPS:
            relationship = "native"
        for field, identifier_type in TRUSTED_IDENTIFIER_FIELDS.items():
            if field not in container:
                continue
            for raw in _values(container.get(field)):
                normalized = normalize_facet_value(raw)
                key = (identifier_type, normalized, relationship)
                if not normalized or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "identifier_type": identifier_type,
                    "identifier_value": normalized,
                    "relationship": relationship,
                    "source_field": f"{prefix}.{field}",
                })
    return tuple(out)


class CharacterDomainResolver:
    """Resolve trusted structured character metadata through the domain registry."""

    def __init__(self, db: Database):
        self.db = db

    async def resolve(
        self,
        *,
        character_id: str,
        source_id,
        card: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        declarations: list[dict[str, Any]] = []
        for item in structured_character_identifiers(card):
            rows = await self.db.fetch(
                """SELECT kd.domain_id, kd.domain_key
                   FROM aios.knowledge_domain_identifier kdi
                   JOIN aios.knowledge_domain kd
                     ON kd.domain_id=kdi.domain_id AND kd.enabled
                   WHERE kdi.identifier_type=$1 AND kdi.identifier_value=$2
                   ORDER BY kdi.confidence DESC, kd.domain_key""",
                item["identifier_type"], item["identifier_value"],
            )
            # Ambiguous identifiers never silently choose a universe.
            if len(rows) == 1:
                row = rows[0]
                declarations.append({
                    "domain": str(row["domain_key"]),
                    "relationship": item["relationship"],
                    "source_field": item["source_field"],
                    "source_fragment": f"{item['identifier_type']}:{item['identifier_value']}",
                    "meta": {
                        "resolved_from_identifier_type": item["identifier_type"],
                        "resolved_from_identifier_value": item["identifier_value"],
                    },
                })
                await self.db.execute(
                    """INSERT INTO aios.character_domain_candidate (
                           character_id, source_id, identifier_type, identifier_value,
                           relationship, status, resolved_domain_id, source_field, meta,
                           resolved_at
                       )
                       VALUES ($1,$2,$3,$4,$5,'resolved',$6,$7,$8::jsonb,now())
                       ON CONFLICT (character_id, source_id, identifier_type,
                                    identifier_value, relationship) DO UPDATE
                       SET status='resolved', resolved_domain_id=EXCLUDED.resolved_domain_id,
                           source_field=EXCLUDED.source_field,
                           meta=aios.character_domain_candidate.meta || EXCLUDED.meta,
                           resolved_at=now()""",
                    character_id, source_id, item["identifier_type"],
                    item["identifier_value"], item["relationship"], row["domain_id"],
                    item["source_field"], json.dumps({"resolver": "domain_registry"}),
                )
            else:
                await self.db.execute(
                    """INSERT INTO aios.character_domain_candidate (
                           character_id, source_id, identifier_type, identifier_value,
                           relationship, status, source_field, meta
                       )
                       VALUES ($1,$2,$3,$4,$5,'unresolved',$6,$7::jsonb)
                       ON CONFLICT (character_id, source_id, identifier_type,
                                    identifier_value, relationship) DO UPDATE
                       SET source_field=EXCLUDED.source_field,
                           meta=aios.character_domain_candidate.meta || EXCLUDED.meta""",
                    character_id, source_id, item["identifier_type"],
                    item["identifier_value"], item["relationship"], item["source_field"],
                    json.dumps({
                        "resolver": "domain_registry",
                        "reason": "unknown_identifier" if not rows else "ambiguous_identifier",
                        "match_count": len(rows),
                    }),
                )
        return declarations


async def reconcile_character_domain_candidates(
    db: Database, *, identifier_type: str, identifier_value: str
) -> int:
    """Retroactively project newly registered structured affiliations.

    Authored character sources use the same authority they had at bootstrap:
    the resolved domain is staged as an identity candidate and accepted through
    the normal revision path. Reference sources remain proposed only.
    """
    from aios_app.char.identity_revision import accept_identity_candidate

    identifier_type = identifier_type.strip().lower()
    value = normalize_facet_value(identifier_value)
    domain_rows = await db.fetch(
        """SELECT kd.domain_id, kd.domain_key
           FROM aios.knowledge_domain_identifier kdi
           JOIN aios.knowledge_domain kd ON kd.domain_id=kdi.domain_id AND kd.enabled
           WHERE kdi.identifier_type=$1 AND kdi.identifier_value=$2
           ORDER BY kdi.confidence DESC, kd.domain_key""",
        identifier_type, value,
    )
    if len(domain_rows) != 1:
        return 0

    domain = domain_rows[0]
    candidates = await db.fetch(
        """SELECT cdc.candidate_id, cdc.character_id, cdc.source_id,
                  cdc.relationship, cdc.source_field, cis.authority
           FROM aios.character_domain_candidate cdc
           LEFT JOIN aios.character_identity_source cis ON cis.source_id=cdc.source_id
           WHERE cdc.identifier_type=$1 AND cdc.identifier_value=$2
             AND cdc.status='unresolved'
           ORDER BY cdc.created_at""",
        identifier_type, value,
    )
    reconciled = 0
    for staged in candidates:
        identity_candidate = await db.execute_returning_row(
            """INSERT INTO aios.character_identity_candidate (
                   character_id, source_id, facet_type, facet_key, value,
                   stability, authority, mutability, perspective,
                   source_field, source_fragment, disposition, meta
               )
               VALUES ($1,$2,'domain',$3,$4::jsonb,'structural',$5,
                       'explicit','self',$6,$7,'proposed',$8::jsonb)
               ON CONFLICT (source_id, facet_type, facet_key, source_field) DO UPDATE
               SET value=EXCLUDED.value,
                   source_fragment=EXCLUDED.source_fragment,
                   meta=aios.character_identity_candidate.meta || EXCLUDED.meta
               RETURNING candidate_id""",
            staged["character_id"], staged["source_id"], domain["domain_key"],
            json.dumps({
                "domain": str(domain["domain_key"]),
                "relationship": str(staged["relationship"]),
            }),
            str(staged["authority"] or "reference"),
            staged["source_field"] or "",
            f"{identifier_type}:{value}",
            json.dumps({
                "resolved_from_identifier_type": identifier_type,
                "resolved_from_identifier_value": value,
                "retroactive_domain_resolution": True,
            }),
        )
        await db.execute(
            """UPDATE aios.character_domain_candidate
               SET status='resolved', resolved_domain_id=$2, resolved_at=now(),
                   meta=meta || jsonb_build_object('projected_identity_candidate',$3::text)
               WHERE candidate_id=$1""",
            staged["candidate_id"], domain["domain_id"],
            str(identity_candidate["candidate_id"]),
        )
        if str(staged["authority"] or "") == "authored":
            await accept_identity_candidate(
                db,
                identity_candidate["candidate_id"],
                actor="domain_registry_reconciliation",
                reason="trusted structured character domain identifier resolved",
            )
        reconciled += 1
    return reconciled
