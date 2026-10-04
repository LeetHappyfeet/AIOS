from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import asyncpg

from aios_app.config import settings


ROOT_DIR = Path(__file__).resolve().parent
BASELINE_SCHEMA = ROOT_DIR / "aios_baseline.sql"
MIGRATIONS_DIR = ROOT_DIR / "migrations" / "current"
BASELINE_RECEIPT = "0001_aios_baseline"
# One canonical ordered chain. Do not squash or edit released SQL/checksums.
# Fresh installs and existing canonical-baseline installs use identical files.
REQUIRED_ACTIVE_MIGRATIONS = frozenset({
    "20260913_baseline_reference_data.sql",
    "20260913_hud_default_profile.sql",
    "20261003_10_integrated_belief_policy_and_integrity.sql",
    "20261003_11_integrity_context_invalidation.sql",
    "20261003_12_occurrence_completion_invalidation.sql",
    "20261003_13_source_receipt_and_belief_hardening.sql",
    "20261003_14_participation_worker_heartbeat.sql",
})
_MIGRATION_LOCK_KEY = "aios.canonical.migrator"
_OUTER_BEGIN = re.compile(r"\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*BEGIN\s*;", re.I | re.S)
_OUTER_COMMIT = re.compile(r"\bCOMMIT\s*;\s*\Z", re.I)


def _migration_body(sql: str, filename: str) -> str:
    """Remove only an optional outer SQL BEGIN/COMMIT wrapper.

    The asyncpg transaction then covers DDL AND the ledger INSERT together.
    Leaving a script's COMMIT inside an asyncpg transaction would silently
    commit the schema BEFORE the receipt, the exact cold-start failure mode
    this migrator must eliminate.
    """
    begin = _OUTER_BEGIN.search(sql)
    commit = _OUTER_COMMIT.search(sql)
    if bool(begin) != bool(commit):
        raise RuntimeError(f"Unbalanced outer transaction in {filename}")
    if begin and commit:
        if commit.start() <= begin.end():
            raise RuntimeError(f"Empty or malformed outer transaction in {filename}")
        return sql[:begin.start()] + sql[begin.end():commit.start()]
    return sql



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
        # PostgreSQL DDL and baseline receipt commit or roll back together.
        async with conn.transaction():
            await conn.execute(baseline_sql)
            await _ensure_migration_ledger(conn)
            await conn.execute(
                """INSERT INTO aios.schema_migration (migration_name, sha256)
                   VALUES ($1,$2)""",
                BASELINE_RECEIPT, baseline_digest,
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
    file_names = {path.name for path in files}
    missing = REQUIRED_ACTIVE_MIGRATIONS - file_names
    if missing:
        raise RuntimeError(f"Canonical migration chain incomplete: {sorted(missing)}")
    # Verify syntax and wrappers BEFORE modifying an empty database.
    prepared = [
        (path, _sha256_text(path.read_text(encoding="utf-8")),
         _migration_body(path.read_text(encoding="utf-8"), path.name))
        for path in files
    ]

    conn = await asyncpg.connect(settings.db_dsn)
    locked = False
    try:
        # Avoid two launch processes racing to initialize the same fresh DB.
        await conn.fetchval("SELECT pg_advisory_lock(hashtext($1)::bigint)", _MIGRATION_LOCK_KEY)
        locked = True
        await _load_or_verify_baseline(conn)
        await _ensure_migration_ledger(conn)

        applied = {
            row["migration_name"]: row["sha256"]
            for row in await conn.fetch(
                "SELECT migration_name, sha256 FROM aios.schema_migration"
            )
        }
        unknown = set(applied) - file_names - {BASELINE_RECEIPT}
        if unknown:
            raise RuntimeError(
                f"Migration ledger contains files absent from canonical chain: {sorted(unknown)}"
            )
        # Reject holes/reordering: a missing older migration cannot be silently
        # installed AFTER a newer receipt just because the file now exists.
        ordered_applied = [path.name for path in files if path.name in applied]
        ordered_prefix = [path.name for path in files[:len(ordered_applied)]]
        if ordered_applied != ordered_prefix:
            raise RuntimeError("Non-prefix migration ledger; rebuild a disposable database or repair explicitly")

        for path, digest, body in prepared:
            previous = applied.get(path.name)
            if previous is not None:
                if previous != digest:
                    raise RuntimeError(
                        f"Migration {path.name} was already applied but its contents "
                        "changed. Applied migrations are immutable; add a new migration instead."
                    )
                print(f"[skip]  {path.name}")
                continue

            print(f"[apply] {path.name}")
            # File body and receipt are a single PostgreSQL transaction even
            # if the historical SQL originally contained BEGIN/COMMIT.
            async with conn.transaction():
                await conn.execute(body)
                await conn.execute(
                    """INSERT INTO aios.schema_migration (migration_name, sha256)
                       VALUES ($1, $2)""",
                    path.name, digest,
                )
            applied[path.name] = digest
            print(f"[done]  {path.name}")

        print(f"Database migrations are up to date ({len(files)} post-baseline files).")
    finally:
        if locked:
            await conn.fetchval(
                "SELECT pg_advisory_unlock(hashtext($1)::bigint)", _MIGRATION_LOCK_KEY
            )
        await conn.close()


def main() -> None:
    asyncio.run(apply_migrations())


if __name__ == "__main__":
    main()
