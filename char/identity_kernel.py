from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from aios_app.db import Database

COMPILER_VERSION = "identity-kernel-v1"

_FACET_ORDER = {
    "identity": 0,
    "appearance": 10,
    "personality": 20,
    "values": 30,
    "expression": 40,
    "constraint": 50,
    "role": 60,
    "developmental": 90,
}


@dataclass(frozen=True)
class CompiledIdentityKernel:
    character_id: str
    identity_version: int
    compiler_version: str
    kernel_text: str
    kernel_json: dict[str, Any]


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, "", [], {}))
    return str(value).strip() if value is not None else ""


def _render(identity: dict[str, Any], facets: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    core = {
        "character_id": identity["character_id"],
        "canonical_name": identity.get("canonical_name"),
        "display_name": identity.get("display_name"),
        "entity_type": identity.get("entity_type"),
        "canon": identity.get("canon"),
        "franchise": identity.get("franchise"),
        "species": identity.get("species"),
        "gender": identity.get("gender"),
        "age_descriptor": identity.get("age_descriptor"),
        "visual_summary": identity.get("visual_summary"),
        "primary_role": identity.get("primary_role"),
        "archetype": identity.get("archetype"),
        "default_tone": identity.get("default_tone"),
        "speech_style": identity.get("speech_style"),
        "moral_constraints": identity.get("moral_constraints"),
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for facet in facets:
        groups.setdefault(str(facet["facet_type"]), []).append(
            {
                "key": facet["facet_key"],
                "value": facet["value"],
                "stability": facet["stability"],
                "authority": facet["authority"],
            }
        )

    name = core.get("display_name") or core.get("canonical_name") or core["character_id"]
    lines = [f"IDENTITY KERNEL — {name}"]
    facts = []
    for key, label in (
        ("canonical_name", "Name"), ("entity_type", "Type"), ("species", "Species"),
        ("gender", "Gender"), ("age_descriptor", "Age"), ("canon", "Canon"),
        ("franchise", "Franchise"),
    ):
        if core.get(key):
            facts.append(f"{label}: {core[key]}")
    if facts:
        lines.extend(facts)

    # Preserve old authored identity fields during migration to facets. They
    # remain identity input, not runtime state, and can be retired after import
    # tooling has materialized equivalent facets for existing characters.
    legacy = []
    for key, label in (
        ("visual_summary", "Appearance"), ("primary_role", "Role"),
        ("archetype", "Archetype"), ("default_tone", "Baseline tone"),
        ("speech_style", "Speech style"), ("moral_constraints", "Constraints"),
    ):
        text = _text_value(core.get(key))
        if text:
            legacy.append(f"{label}: {text}")
    if legacy:
        lines.append("AUTHORED BASELINE: " + " | ".join(legacy))

    for facet_type in sorted(groups, key=lambda k: (_FACET_ORDER.get(k, 70), k)):
        values = groups[facet_type]
        rendered = []
        for item in sorted(values, key=lambda x: x["key"]):
            text = _text_value(item["value"])
            if text:
                rendered.append(text if item["key"] in {"summary", "description"} else f"{item['key']}: {text}")
        if rendered:
            lines.append(f"{facet_type.replace('_', ' ').upper()}: " + " | ".join(rendered))

    payload = {"core": core, "facets": groups}
    return payload, "\n".join(lines)


class IdentityKernelStore:
    """Compile/cache the deterministic, character-scoped identity substrate."""

    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[tuple[str, int, str], CompiledIdentityKernel] = {}

    async def get(self, character_id: str) -> Optional[CompiledIdentityKernel]:
        identity_row = await self.db.fetchrow(
            "SELECT * FROM aios.character_identity WHERE character_id=$1",
            character_id,
        )
        if not identity_row:
            return None
        identity = dict(identity_row)
        version = int(identity.get("identity_version") or 1)
        key = (character_id, version, COMPILER_VERSION)
        if key in self._cache:
            return self._cache[key]

        row = await self.db.fetchrow(
            """
            SELECT kernel_json, kernel_text
            FROM aios.compiled_identity_kernel
            WHERE character_id=$1 AND identity_version=$2 AND compiler_version=$3
            """,
            character_id, version, COMPILER_VERSION,
        )
        if row:
            kernel = CompiledIdentityKernel(
                character_id=character_id,
                identity_version=version,
                compiler_version=COMPILER_VERSION,
                kernel_text=row["kernel_text"],
                kernel_json=dict(row["kernel_json"] or {}),
            )
            self._cache[key] = kernel
            return kernel
        return await self.compile(character_id)

    async def compile(self, character_id: str) -> CompiledIdentityKernel:
        identity_row = await self.db.fetchrow(
            "SELECT * FROM aios.character_identity WHERE character_id=$1",
            character_id,
        )
        if not identity_row:
            raise ValueError(f"unknown character {character_id}")
        identity = dict(identity_row)
        version = int(identity.get("identity_version") or 1)
        rows = await self.db.fetch(
            """
            SELECT facet_type, facet_key, value, stability, authority
            FROM aios.character_identity_facet
            WHERE character_id=$1 AND status='active'
            ORDER BY facet_type, facet_key
            """,
            character_id,
        )
        payload, text = _render(identity, [dict(row) for row in rows])
        payload["identity_version"] = version
        payload["compiler_version"] = COMPILER_VERSION
        await self.db.execute(
            """
            INSERT INTO aios.compiled_identity_kernel (
                character_id, identity_version, compiler_version, kernel_json, kernel_text
            )
            VALUES ($1,$2,$3,$4::jsonb,$5)
            ON CONFLICT (character_id, identity_version, compiler_version) DO UPDATE
            SET kernel_json=EXCLUDED.kernel_json,
                kernel_text=EXCLUDED.kernel_text,
                compiled_at=now()
            """,
            character_id, version, COMPILER_VERSION, json.dumps(payload), text,
        )
        kernel = CompiledIdentityKernel(character_id, version, COMPILER_VERSION, text, payload)
        self._cache[(character_id, version, COMPILER_VERSION)] = kernel
        return kernel

    def invalidate(self, character_id: str) -> None:
        for key in list(self._cache):
            if key[0] == character_id:
                self._cache.pop(key, None)
