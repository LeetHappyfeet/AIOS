"""Canonical causal schema smoke after prototype-era migrations were collapsed.

There is no supported in-place upgrade of legacy prototype runtime tables.
Fresh installations must obtain these contracts directly from aios_baseline.sql.
"""
import asyncio
import os
from pathlib import Path

import asyncpg


async def main() -> None:
    conn = await asyncpg.connect(os.environ["AIOS_DB_DSN"])
    try:
        await conn.execute(Path("aios_baseline.sql").read_text(encoding="utf-8"))
        tables = await conn.fetch(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='aios' AND table_name IN
                 ('causal_candidate','causal_admission','causal_state')
               ORDER BY table_name"""
        )
        assert [r["table_name"] for r in tables] == [
            "causal_admission", "causal_candidate", "causal_state",
        ]
        columns = await conn.fetch(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='aios' AND table_name='world_event'
                 AND column_name IN ('candidate_id','domain_id','result_state_version')
               ORDER BY column_name"""
        )
        assert [r["column_name"] for r in columns] == [
            "candidate_id", "domain_id", "result_state_version",
        ]
        print("PASS: canonical baseline includes causal kernel and runtime event columns")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
