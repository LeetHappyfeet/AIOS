from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import asyncpg

from aios_app.config import settings


ROOT_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT_DIR / "migrations"
BASE_SCHEMA = ROOT_DIR / "aios_schema.sql"


def _strip_psql_meta_commands(sql: str) -> str:
    """Remove pg_dump psql-only backslash commands before asyncpg execution."""
    return "\n".join(
        line for line in sql.splitlines()
        if not line.lstrip().startswith("\\")
    )


async def apply_migrations() -> None:
    if not MIGRATIONS_DIR.exists():
        raise RuntimeError(f"Missing migrations directory: {MIGRATIONS_DIR}")

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("No migrations found.")
        return

    conn = await asyncpg.connect(settings.db_dsn)
    try:
        base_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema='aios'
                  AND table_name='character_identity'
            )
            """
        )

        if not base_exists:
            if not BASE_SCHEMA.exists():
                raise RuntimeError(
                    "AIOS base schema is missing and aios_schema.sql was not found."
                )
            print("[base]  Loading aios_schema.sql")
            base_sql = _strip_psql_meta_commands(
                BASE_SCHEMA.read_text(encoding="utf-8")
            )
            await conn.execute(base_sql)
            print("[done]  Base schema loaded")

        await conn.execute(
            """
            CREATE SCHEMA IF NOT EXISTS aios;

            CREATE TABLE IF NOT EXISTS aios.schema_migration (
                migration_name text PRIMARY KEY,
                sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            );
            """
        )

        applied = {
            row["migration_name"]: row["sha256"]
            for row in await conn.fetch(
                "SELECT migration_name, sha256 FROM aios.schema_migration"
            )
        }

        for path in files:
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            previous = applied.get(path.name)

            if previous:
                if previous != digest:
                    raise RuntimeError(
                        f"Migration {path.name} was already applied but its contents changed. "
                        "Do not silently reapply edited migrations; add a new migration instead."
                    )
                print(f"[skip]  {path.name}")
                continue

            print(f"[apply] {path.name}")
            # Migration files own their BEGIN/COMMIT boundaries.
            await conn.execute(sql)
            await conn.execute(
                """
                INSERT INTO aios.schema_migration (migration_name, sha256)
                VALUES ($1,$2)
                """,
                path.name,
                digest,
            )
            print(f"[done]  {path.name}")

        print("Database migrations are up to date.")
    finally:
        await conn.close()


def main() -> None:
    asyncio.run(apply_migrations())


if __name__ == "__main__":
    main()
