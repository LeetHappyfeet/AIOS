"""Real cold-start acceptance gate. Uses a disposable PostgreSQL 16 database.

Unlike selected-migration smokes, this runs aios_app.migrate over EVERY
current migration in exactly the production order, reruns it, verifies the
effective executor and proves a failing migration cannot leave orphan DDL.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import asyncpg

from aios_app import migrate
from aios_app.db_check import check_database
from aios_app.epistemic.runtime_versions import (
    capture_runtime_manifest, BELIEF_EXECUTOR_VERSION, SOURCE_CONTRACT_VERSION,
)


async def main() -> None:
    await migrate.apply_migrations()
    assert await check_database() == 0, "cold-start DB preflight did not pass"
    conn = await asyncpg.connect(os.environ["AIOS_DB_DSN"])
    try:
        files = sorted(migrate.MIGRATIONS_DIR.glob("*.sql"))
        expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        async def ledger():
            rows = await conn.fetch(
                "SELECT migration_name, sha256 FROM aios.schema_migration"
            )
            return {r["migration_name"]: r["sha256"] for r in rows}

        first = await ledger()
        assert first == {
            "0001_aios_baseline": migrate._sha256_text(
                migrate.BASELINE_SCHEMA.read_text(encoding="utf-8")
            ), **expected,
        }, "full canonical migration set and hashes must match exactly"
        manifest = await capture_runtime_manifest(conn)
        assert manifest["effective_authority"]["belief_executor"] == BELIEF_EXECUTOR_VERSION
        assert manifest["effective_authority"]["source_integrity_contract"] == SOURCE_CONTRACT_VERSION
        assert manifest["effective_authority"]["checks"]["required_migrations_applied"] is True
        assert await conn.fetchval(
            "SELECT to_regclass('aios.character_participation_worker_heartbeat') IS NOT NULL"
        )
        print("PASS full migration chain, effective authority, strict V4, worker readiness", flush=True)

        # Production migrator must be a safe no-op on the same immutable chain.
        await migrate.apply_migrations()
        assert await ledger() == first
        print("PASS rerun: all migration receipts stable", flush=True)

        # A second temporary active directory with one failing additive file
        # proves schema DDL and its receipt share a transaction. No direct
        # operator data, existing migration or live service is touched.
        with TemporaryDirectory(prefix="aios-canonical-cold-start-") as td:
            shadow = Path(td) / "current"
            shutil.copytree(migrate.MIGRATIONS_DIR, shadow)
            bad = shadow / "99999999_atomic_receipt_probe.sql"
            bad.write_text(
                "BEGIN;\nCREATE TABLE aios.__migration_atomicity_probe(x integer);\n"
                "INSERT INTO aios.__intentionally_missing_relation(x) VALUES(1);\n"
                "COMMIT;\n",
                encoding="utf-8",
            )
            original = migrate.MIGRATIONS_DIR
            try:
                migrate.MIGRATIONS_DIR = shadow
                try:
                    await migrate.apply_migrations()
                except asyncpg.UndefinedTableError:
                    pass
                else:
                    raise AssertionError("Intentionally failing migration unexpectedly succeeded")
            finally:
                migrate.MIGRATIONS_DIR = original

        assert await conn.fetchval(
            "SELECT to_regclass('aios.__migration_atomicity_probe') IS NULL"
        ), "DDL survived despite failed migration transaction"
        assert not await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM aios.schema_migration WHERE migration_name=$1)",
            "99999999_atomic_receipt_probe.sql",
        ), "failed migration received a ledger entry"
        assert await ledger() == first
        print("PASS invalid migration rolls back both DDL and ledger; no history hole", flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
