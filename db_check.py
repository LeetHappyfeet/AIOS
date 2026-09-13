from __future__ import annotations

import asyncio

import asyncpg

from aios_app.config import settings


BASELINE_RECEIPT = "0001_aios_baseline"

REQUIRED_TABLES = {
    "character_identity",
    "character_instance",
    "character_runtime_state",
    "character_hud_readiness",
    "character_epistemic_profile",
    "character_proposition_knowledge",
    "source_identity",
    "semantic_topology_node",
    "semantic_topology_edge",
    "semantic_topology_projection",
    "semantic_anchor_edge",
    "semantic_neighbor_relation",
    "semantic_cluster_candidate",
    "semantic_cluster_membership",
    "semantic_cluster_boundary",
    "semantic_cluster_classification",
    "semantic_boundary_classification",
    "semantic_reconciliation_receipt",
    "semantic_branch_candidate",
    "world",
    "world_entity",
    "world_rule",
    "world_event",
    "proposition",
    "observation",
    "proposition_evidence",
    "proposition_conflict",
    "narrative_cluster",
    "knowledge_acquisition_event",
    "world_proposition_assertion",
    "document_unit",
    "document_metadata_observation",
}


async def check_database() -> int:
    print(f"PostgreSQL DSN: {settings.db_dsn}")
    try:
        conn = await asyncpg.connect(settings.db_dsn, timeout=5)
    except Exception as exc:
        print(f"FAIL: PostgreSQL connection failed: {exc}")
        return 1

    try:
        version = await conn.fetchval("SELECT version()")
        print(f"OK: {version}")

        schema_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='aios')"
        )
        if not schema_exists:
            print("FAIL: schema 'aios' does not exist.")
            print("Run: python -m aios_app.migrate")
            return 2

        rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema='aios'
              AND table_type='BASE TABLE'
            """
        )
        present = {r["table_name"] for r in rows}
        missing = sorted(REQUIRED_TABLES - present)

        print(f"AIOS tables present: {len(present)}")
        if missing:
            print("FAIL: required tables are missing:")
            for name in missing:
                print(f"  - {name}")
            print("Run: python -m aios_app.migrate")
            return 3

        if "schema_migration" not in present:
            print("FAIL: migration ledger is missing.")
            print("Run: python -m aios_app.migrate")
            return 4

        baseline = await conn.fetchrow(
            """
            SELECT migration_name, sha256, applied_at
            FROM aios.schema_migration
            WHERE migration_name=$1
            """,
            BASELINE_RECEIPT,
        )
        if baseline is None:
            print("FAIL: database is not on the canonical AIOS baseline line.")
            print("This looks like a prototype-era database; use a fresh database with this branch.")
            return 5

        print(
            f"OK: baseline {baseline['migration_name']}  {baseline['applied_at']}"
        )

        rows = await conn.fetch(
            """
            SELECT migration_name, applied_at
            FROM aios.schema_migration
            WHERE migration_name <> $1
            ORDER BY migration_name
            """,
            BASELINE_RECEIPT,
        )
        if rows:
            print("Post-baseline migrations:")
            for row in rows:
                print(f"  {row['migration_name']}  {row['applied_at']}")
        else:
            print("Post-baseline migrations: none")

        liminal = await conn.fetchrow(
            "SELECT world_id, world_key FROM aios.world WHERE world_key='liminal'"
        )
        if liminal:
            print(f"OK: liminal world {liminal['world_id']}")
        else:
            print("WARN: no world with world_key='liminal'; ingestion will need one.")

        print("Database structure looks ready.")
        return 0
    finally:
        await conn.close()


def main() -> None:
    raise SystemExit(asyncio.run(check_database()))


if __name__ == "__main__":
    main()
