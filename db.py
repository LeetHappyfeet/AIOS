# aios/db.py

import json
import asyncpg
from typing import Optional, Any, Dict
from uuid import UUID


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    # -------------------------------------------------
    # Connection management
    # -------------------------------------------------

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=1,
            max_size=10,
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    # -------------------------------------------------
    # Basic query helpers
    # -------------------------------------------------

    async def fetchrow(self, sql: str, *args):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetch(sql, *args)

    async def execute(self, sql: str, *args):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.execute(sql, *args)

    async def execute_returning_row(self, sql: str, *args):
        """Execute a statement that returns a single row."""
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetchrow(sql, *args)

    # -------------------------------------------------
    # Ingest event creation (IDEMPOTENT)
    # -------------------------------------------------

    async def create_ingest_event(
        self,
        *,
        source: str,
        source_event_id: str,
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        dedupe_key: Optional[str] = None,
    ) -> int:
        """
        Create or retrieve an ingest_event.

        This function is fully idempotent:
        - If the event already exists (same dedupe_key),
          it returns the existing event_id.
        - If not, it inserts and returns the new event_id.
        """

        payload = payload or {}
        dedupe_key = dedupe_key or f"{source}:{source_event_id}:{kind}"

        # 1. Attempt insert
        row = await self.execute_returning_row(
            """
            INSERT INTO aios.ingest_event (
                source,
                source_event_id,
                kind,
                session_id,
                speaker_id,
                speaker_role,
                recipient_id,
                character_id,
                user_name,
                message_text,
                payload,
                dedupe_key
            )
            VALUES (
                $1,
                $2,
                $3::aios.event_kind,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                $4::jsonb,
                $5
            )
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING event_id
            """,
            source,
            source_event_id,
            kind,
            json.dumps(payload),
            dedupe_key,
        )

        if row:
            # Successfully inserted
            return int(row["event_id"])

        # 2. Conflict case: fetch existing event_id
        row = await self.fetchrow(
            """
            SELECT event_id
            FROM aios.ingest_event
            WHERE dedupe_key = $1
            """,
            dedupe_key,
        )

        if not row:
            # This should never happen unless the DB is corrupted
            raise RuntimeError(
                f"Ingest event dedupe_key '{dedupe_key}' exists but could not be fetched"
            )

        return int(row["event_id"])
