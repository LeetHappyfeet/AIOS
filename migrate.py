from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import asyncpg

from aios_app.config import settings


ROOT_DIR = Path(__file__).resolve().parent
BASELINE_SCHEMA = ROOT_DIR / "aios_baseline.sql"
MIGRATIONS_DIR = ROOT_DIR / "migrations" / "current"
BASELINE_RECEIPT = "0001_aios_baseline"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _aios_tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='aios'
          AND table_type='BASE TABLE'
        """
    )
    return {row["table_name"] for row in rows}


async def _ensure_migration_ledger(conn: asyncpg.Connection) -> None:
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


async def _load_or_verify_baseline(conn: asyncpg.Connection) -> None:
    if not BASELINE_SCHEMA.exists():
        raise RuntimeError(
            f"Canonical AIOS baseline is missing: {BASELINE_SCHEMA}"
        )

    baseline_sql = BASELINE_SCHEMA.read_text(encoding="utf-8")
    baseline_digest = _sha256_text(baseline_sql)
    tables = await _aios_tables(conn)

    if not tables:
        print("[base]  Loading aios_baseline.sql")
        await conn.execute(baseline_sql)
        await _ensure_migration_ledger(conn)
        await conn.execute(
            """
            INSERT INTO aios.schema_migration (migration_name, sha256)
            VALUES ($1, $2)
            ON CONFLICT (migration_name) DO NOTHING
            """,
            BASELINE_RECEIPT,
            baseline_digest,
        )
        print("[done]  AIOS baseline loaded")
        return

    if "schema_migration" not in tables:
        raise RuntimeError(
            "Existing AIOS tables were found, but this database has no baseline "
            "migration ledger. This branch no longer upgrades prototype-era schemas. "
            "Use a fresh database for the new baseline, or restore/use the old branch "
            "for the historical database."
        )

    baseline_previous = await conn.fetchval(
        """
        SELECT sha256
        FROM aios.schema_migration
        WHERE migration_name=$1
        """,
        BASELINE_RECEIPT,
    )

    if baseline_previous is None:
        raise RuntimeError(
            "A pre-baseline AIOS database was detected. Historical prototype migrations "
            "are intentionally retired on this branch. Point AIOS at a fresh database "
            "and run python -m aios_app.migrate to initialize it from aios_baseline.sql."
        )

    if baseline_previous != baseline_digest:
        raise RuntimeError(
            "aios_baseline.sql has changed since this database was initialized. "
            "The baseline is immutable; restore the released baseline or rebuild the "
            "database deliberately."
        )

    print("[skip]  aios_baseline.sql")


async def apply_migrations() -> None:
    if not MIGRATIONS_DIR.exists():
        raise RuntimeError(f"Missing active migrations directory: {MIGRATIONS_DIR}")

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    conn = await asyncpg.connect(settings.db_dsn)
    try:
        await _load_or_verify_baseline(conn)
        await _ensure_migration_ledger(conn)

        applied = {
            row["migration_name"]: row["sha256"]
            for row in await conn.fetch(
                "SELECT migration_name, sha256 FROM aios.schema_migration"
            )
        }

        for path in files:
            sql = path.read_text(encoding="utf-8")
            digest = _sha256_text(sql)
            previous = applied.get(path.name)

            if previous:
                if previous != digest:
                    raise RuntimeError(
                        f"Migration {path.name} was already applied but its contents "
                        "changed. Applied migrations are immutable; add a new migration "
                        "instead."
                    )
                print(f"[skip]  {path.name}")
                continue

            print(f"[apply] {path.name}")
            await conn.execute(sql)
            await conn.execute(
                """
                INSERT INTO aios.schema_migration (migration_name, sha256)
                VALUES ($1, $2)
                """,
                path.name,
                digest,
            )
            applied[path.name] = digest
            print(f"[done]  {path.name}")

        if not files:
            print("No post-baseline migrations to apply.")

        print("Database migrations are up to date.")
    finally:
        await conn.close()


def main() -> None:
    asyncio.run(apply_migrations())


if __name__ == "__main__":
    main()
