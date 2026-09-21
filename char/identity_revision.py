from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.char.identity_kernel import IdentityKernelStore, _json_value


async def accept_identity_candidate(
    db: Database,
    candidate_id: UUID | str,
    *,
    actor: str = "system",
    reason: str | None = None,
) -> dict[str, Any]:
    """Promote one staged candidate into durable identity and version the kernel."""
    async with db.connection() as con:
        async with con.transaction():
            candidate = await con.fetchrow(
                """
                SELECT *
                FROM aios.character_identity_candidate
                WHERE candidate_id=$1::uuid
                FOR UPDATE
                """,
                str(candidate_id),
            )
            if not candidate:
                raise LookupError(f"unknown identity candidate {candidate_id}")
            if candidate["disposition"] == "rejected":
                raise ValueError("rejected identity candidate cannot be accepted")
            character_id = candidate["character_id"]

            identity = await con.fetchrow(
                """
                SELECT identity_version
                FROM aios.character_identity
                WHERE character_id=$1
                FOR UPDATE
                """,
                character_id,
            )
            if not identity:
                raise LookupError(f"unknown character {character_id}")

            existing = await con.fetchrow(
                """
                SELECT facet_id, value
                FROM aios.character_identity_facet
                WHERE character_id=$1 AND facet_type=$2 AND facet_key=$3
                FOR UPDATE
                """,
                character_id, candidate["facet_type"], candidate["facet_key"],
            )
            previous_value = _json_value(existing["value"]) if existing else None

            if (
                candidate["disposition"] == "accepted"
                and candidate["accepted_facet_id"] is not None
                and existing
                and existing["facet_id"] == candidate["accepted_facet_id"]
                and existing["value"] == candidate["value"]
            ):
                return {
                    "character_id": character_id,
                    "candidate_id": str(candidate["candidate_id"]),
                    "facet_id": str(existing["facet_id"]),
                    "identity_version": int(identity["identity_version"]),
                    "changed": False,
                }

            facet = await con.fetchrow(
                """
                INSERT INTO aios.character_identity_facet (
                    character_id, facet_type, facet_key, value, stability,
                    authority, mutability, source_id, source_field,
                    source_fragment, status, meta
                )
                VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10,'active',$11::jsonb)
                ON CONFLICT (character_id, facet_type, facet_key) DO UPDATE
                SET value=EXCLUDED.value,
                    stability=EXCLUDED.stability,
                    authority=EXCLUDED.authority,
                    mutability=EXCLUDED.mutability,
                    source_id=EXCLUDED.source_id,
                    source_field=EXCLUDED.source_field,
                    source_fragment=EXCLUDED.source_fragment,
                    status='active',
                    meta=aios.character_identity_facet.meta || EXCLUDED.meta,
                    updated_at=now()
                RETURNING facet_id, value
                """,
                character_id,
                candidate["facet_type"],
                candidate["facet_key"],
                json.dumps(_json_value(candidate["value"])),
                candidate["stability"],
                candidate["authority"],
                candidate["mutability"],
                candidate["source_id"],
                candidate["source_field"],
                candidate["source_fragment"],
                json.dumps({
                    "perspective": candidate["perspective"],
                    "continuity_key": candidate["continuity_key"],
                    "candidate_id": str(candidate["candidate_id"]),
                }),
            )

            version_row = await con.fetchrow(
                """
                UPDATE aios.character_identity
                SET identity_version=identity_version+1, updated_at=now()
                WHERE character_id=$1
                RETURNING identity_version
                """,
                character_id,
            )
            version = int(version_row["identity_version"])

            await con.execute(
                """
                UPDATE aios.character_identity_candidate
                SET disposition='accepted',
                    accepted_facet_id=$2,
                    decided_at=now()
                WHERE candidate_id=$1::uuid
                """,
                str(candidate_id), facet["facet_id"],
            )
            await con.execute(
                """
                INSERT INTO aios.character_identity_revision (
                    character_id, identity_version, operation, source_id,
                    candidate_id, facet_id, previous_value, new_value,
                    actor, reason, meta
                )
                VALUES ($1,$2,'accept_candidate',$3,$4::uuid,$5,$6::jsonb,$7::jsonb,$8,$9,
                        jsonb_build_object('perspective',$10,'continuity_key',$11))
                """,
                character_id, version, candidate["source_id"], str(candidate_id),
                facet["facet_id"],
                json.dumps(previous_value) if previous_value is not None else None,
                json.dumps(_json_value(candidate["value"])), actor, reason,
                candidate["perspective"], candidate["continuity_key"],
            )

    kernel = await IdentityKernelStore(db).compile(character_id)
    return {
        "character_id": character_id,
        "candidate_id": str(candidate_id),
        "facet_id": str(facet["facet_id"]),
        "identity_version": kernel.identity_version,
        "changed": True,
        "kernel_text": kernel.kernel_text,
    }


async def reject_identity_candidate(
    db: Database,
    candidate_id: UUID | str,
    *,
    reason: str | None = None,
) -> None:
    result = await db.execute(
        """
        UPDATE aios.character_identity_candidate
        SET disposition='rejected',
            decided_at=now(),
            meta=meta || jsonb_build_object('rejection_reason',$2)
        WHERE candidate_id=$1::uuid AND disposition='proposed'
        """,
        str(candidate_id), reason,
    )
    if result.endswith(" 0"):
        raise LookupError(f"no proposed identity candidate {candidate_id}")
