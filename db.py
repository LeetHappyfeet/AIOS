import json
import asyncpg
from typing import Optional, Any, Dict, List
from uuid import UUID


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(dsn=self._dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

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
        """Helper when you want a single RETURNING row."""
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetchrow(sql, *args)

    async def create_ingest_event(
        self,
        *,
        source: str,
        source_event_id: str,
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        dedupe_key: Optional[str] = None,
    ) -> int:
        payload = payload or {}
        dedupe_key = dedupe_key or f"{source}:{source_event_id}:{kind}"

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
            RETURNING event_id
            """,
            source,
            source_event_id,
            kind,
            json.dumps(payload),
            dedupe_key,
        )
        return int(row["event_id"])
