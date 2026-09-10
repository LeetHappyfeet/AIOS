from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import asyncpg

from aios_app.config import settings


ROOT_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT_DIR / "migrations"
BASE_SCHEMA = ROOT_DIR / "aios_schema.sql"
LEGACY_MIGRATIONS_DIR = MIGRATIONS_DIR / "legacy"

# Domain checkpoints replace the prototype-era patch chain.  Existing databases
# may already have any/all of these historical receipts.  The legacy SQL is kept
# verbatim under migrations/legacy so checksums and interrupted upgrades remain
# recoverable without keeping the old files in the active migration namespace.
DOMAIN_SUPERSEDES: dict[str, tuple[str, ...]] = {
    "20260909_domain_01_ingestion_provenance.sql": (
        "20260903_ingestion_state.sql",
        "20260906_claim_context_resolution.sql",
        "20260906_zzzz_character_hud_profiles.sql",
        "20260906_zzzzz_external_observation_provenance.sql",
        "20260906_zzzzzzzz_source_event_supersession.sql",
    ),
    "20260909_domain_02_pipeline_scheduler.sql": (
        "20260903_pipeline_job_identity.sql",
        "20260905_zzzz_pipeline_claim_job_identity.sql",
        "20260906_zzzzzzz_pipeline_topology_job_identity.sql",
        "20260908_parallel_execution_scheduler.sql",
        "20260909_pipeline_job_identity_normalize.sql",
        "20260909_semantic_scheduler_fairness.sql",
    ),
    "20260909_domain_03_epistemic_runtime.sql": (
        "20260905_epistemic_control.sql",
        "20260905_world_runtime.sql",
        "20260905_zz_epistemic_runtime_links.sql",
        "20260905_zzz_longdoc_character_epistemics.sql",
        "20260906_zz_character_world_topology.sql",
        "20260906_zzz_runtime_source_cursor.sql",
    ),
    "20260909_domain_04_documents.sql": (
        "20260905_zzz_longdoc_character_epistemics.sql",
    ),
    "20260909_domain_05_hud.sql": (
        "20260906_zzzz_character_hud_profiles.sql",
        "20260906_zzzzzzz_hud_generation_readiness.sql",
    ),
    "20260909_domain_06_semantic_engine.sql": (
        "20260906_semantic_index.sql",
        "20260906_zzzzzz_semantic_topology.sql",
        "20260906_zzzzzzz_remove_legacy_rag.sql",
        "20260906_zzzzzzz_semantic_anchor_edges.sql",
        "20260906_zzzzzzzz_semantic_clustering.sql",
        "20260906_zzzzzzzzz_semantic_classifier.sql",
        "20260906_zzzzzzzzzz_semantic_neighbor_classifier.sql",
        "20260906_zzzzzzzzzzz_semantic_reconciliation.sql",
        "20260908_semantic_qdrant_tree_rebuild.sql",
        "20260909_semantic_proposition_leaves.sql",
    ),
}


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

            legacy_names = DOMAIN_SUPERSEDES.get(path.name, ())
            legacy_present = [name for name in legacy_names if name in applied]

            if legacy_present:
                # A pre-checkpoint database is on the historical migration line.
                # Verify already-applied hashes and finish any interrupted legacy
                # domain before recording the consolidated checkpoint receipt.
                for legacy_name in legacy_names:
                    legacy_path = LEGACY_MIGRATIONS_DIR / legacy_name
                    if not legacy_path.exists():
                        raise RuntimeError(
                            f"Missing legacy migration required by {path.name}: "
                            f"{legacy_path}"
                        )

                    legacy_sql = legacy_path.read_text(encoding="utf-8")
                    legacy_digest = hashlib.sha256(
                        legacy_sql.encode("utf-8")
                    ).hexdigest()
                    legacy_previous = applied.get(legacy_name)

                    if legacy_previous:
                        if legacy_previous != legacy_digest:
                            raise RuntimeError(
                                f"Legacy migration {legacy_name} was already applied "
                                "but its archived contents changed."
                            )
                        continue

                    print(f"[compat] {legacy_name}")
                    await conn.execute(legacy_sql)
                    await conn.execute(
                        """
                        INSERT INTO aios.schema_migration (migration_name, sha256)
                        VALUES ($1,$2)
                        """,
                        legacy_name,
                        legacy_digest,
                    )
                    applied[legacy_name] = legacy_digest
                    print(f"[done]  {legacy_name}")

                await conn.execute(
                    """
                    INSERT INTO aios.schema_migration (migration_name, sha256)
                    VALUES ($1,$2)
                    """,
                    path.name,
                    digest,
                )
                applied[path.name] = digest
                print(
                    f"[skip]  {path.name} "
                    "(legacy domain verified/converged)"
                )
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
