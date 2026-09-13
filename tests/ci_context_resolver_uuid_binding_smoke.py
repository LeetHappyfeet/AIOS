from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg


UPDATE_SQL = """
UPDATE aios.claim_context_resolution
SET character_instance_id=$2::uuid,
    meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
        'character_instance_id', ($2::uuid)::text,
        'runtime_binding_repaired', true
    ),
    resolved_at=now()
WHERE claim_id=$1
"""


async def main() -> None:
    conn = await asyncpg.connect(os.environ["AIOS_DB_DSN"])
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aios.claim_context_resolution (
                claim_id uuid PRIMARY KEY,
                character_instance_id uuid,
                meta jsonb NOT NULL DEFAULT '{}'::jsonb,
                resolved_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        claim_id = uuid4()
        instance_id = uuid4()
        await conn.execute(
            "INSERT INTO aios.claim_context_resolution(claim_id) VALUES ($1)",
            claim_id,
        )

        await conn.execute(UPDATE_SQL, claim_id, instance_id)
        row = await conn.fetchrow(
            """
            SELECT character_instance_id,
                   meta->>'character_instance_id' AS meta_instance_id,
                   meta->>'runtime_binding_repaired' AS repaired
            FROM aios.claim_context_resolution
            WHERE claim_id=$1
            """,
            claim_id,
        )

        assert row is not None
        assert row["character_instance_id"] == instance_id
        assert row["meta_instance_id"] == str(instance_id)
        assert row["repaired"] == "true"
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
