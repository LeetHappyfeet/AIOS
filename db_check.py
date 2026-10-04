from __future__ import annotations

import asyncio

import asyncpg

from aios_app.config import settings


BASELINE_RECEIPT = "0001_aios_baseline"
REFERENCE_DATA_MIGRATION = "20260913_baseline_reference_data.sql"
HUD_DEFAULT_PROFILE_MIGRATION = "20260913_hud_default_profile.sql"

REQUIRED_TABLES = {
    "character_identity",
    "character_instance",
    "character_runtime_state",
    "character_hud_readiness",
    "character_epistemic_profile",
    "character_proposition_knowledge",
    "hud_profile",
    "character_hud_profile",
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
    "reconciliation_family_policy",
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

        print(f"OK: baseline {baseline['migration_name']}  {baseline['applied_at']}")

        reference_receipt = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM aios.schema_migration
                WHERE migration_name=$1
            )
            """,
            REFERENCE_DATA_MIGRATION,
        )
        if not reference_receipt:
            print(f"FAIL: required reference-data migration is missing: {REFERENCE_DATA_MIGRATION}")
            print("Run: python -m aios_app.migrate")
            return 6

        policy_count = await conn.fetchval(
            "SELECT count(*) FROM aios.reconciliation_family_policy"
        )
        required_policy_count = await conn.fetchval(
            """
            SELECT count(*)
            FROM aios.reconciliation_family_policy
            WHERE predicate_family = ANY($1::text[])
            """,
            [
                "UNKNOWN", "IDENTITY", "SOCIAL", "MEMBERSHIP", "POSSESSION",
                "EPISTEMIC", "MEMORY", "CAUSAL", "COMMUNICATION", "ACTION",
                "TEMPORAL", "DESCRIPTIVE", "EMOTIONAL", "GOAL", "SPATIAL", "RULE",
            ],
        )
        if policy_count < 16 or required_policy_count != 16:
            print(
                "FAIL: reconciliation policy reference data is incomplete "
                f"({required_policy_count}/16 required families present)."
            )
            print("Run: python -m aios_app.migrate")
            return 7
        print("OK: reconciliation policies 16/16")

        # A schema can have all its tables while executing the wrong resolver.
        # Verify the integration migration, mandatory default policy, and the
        # actual SQL wrapper selected by serialized reconciliation at startup.
        integrated_receipt = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM aios.schema_migration WHERE migration_name=$1)",
            "20261003_10_integrated_belief_policy_and_integrity.sql",
        )
        context_receipt = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM aios.schema_migration WHERE migration_name=$1)",
            "20261003_11_integrity_context_invalidation.sql",
        )
        completion_receipt = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM aios.schema_migration WHERE migration_name=$1)",
            "20261003_12_occurrence_completion_invalidation.sql",
        )
        hardening_receipt = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM aios.schema_migration WHERE migration_name=$1)",
            "20261003_13_source_receipt_and_belief_hardening.sql",
        )
        participation_receipt = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM aios.schema_migration WHERE migration_name=$1)",
            "20261003_14_participation_worker_heartbeat.sql",
        )
        if not integrated_receipt or not context_receipt or not completion_receipt or not hardening_receipt:
            print("FAIL: integrated belief/source integrity migrations missing.")
            print("Run: python -m aios_app.migrate")
            return 10
        if not participation_receipt:
            print("FAIL: participation shadow readiness migration missing.")
            print("Run: python -m aios_app.migrate")
            return 16
        heartbeat_relation = await conn.fetchval(
            "SELECT to_regclass('aios.character_participation_worker_heartbeat') IS NOT NULL"
        )
        if not heartbeat_relation:
            print("FAIL: participation shadow evaluator heartbeat relation is absent.")
            return 17
        try:
            await conn.execute("SELECT aios.assert_belief_policy_configuration()")
        except Exception as exc:
            print(f"FAIL: belief policy runtime contract: {exc}")
            return 11
        resolver_body = await conn.fetchval(
            """SELECT pg_get_functiondef(
                to_regprocedure('aios.reconcile_character_belief_atom(uuid,uuid)'))"""
        )
        if not resolver_body or (
            "reconcile_character_belief_atom_authority_v3" not in resolver_body
            or "apply_character_belief_policy" not in resolver_body
        ):
            print("FAIL: serialized belief resolver is not authority+family integrated.")
            return 12
        gates = await conn.fetchval(
            """SELECT count(*) FROM pg_proc p
               JOIN pg_namespace n ON n.oid=p.pronamespace
               WHERE n.nspname='aios' AND p.proname IN (
                 'semantic_integrity_claim_current',
                 'semantic_occurrence_topology_eligible',
                 'semantic_acquisition_source_eligible'
               )"""
        )
        if gates != 3:
            print(f"FAIL: missing authoritative integrity/source gates ({gates}/3).")
            return 13
        # Migration presence alone is insufficient: verify that the effective
        # read gate is strict about V4 source identity and both active belief
        # materializers invoke the shared acquisition source contract.
        gate_body = await conn.fetchval(
            """SELECT pg_get_functiondef(
               to_regprocedure('aios.semantic_integrity_claim_current(uuid)'))"""
        )
        authority_body = await conn.fetchval(
            """SELECT pg_get_functiondef(
               to_regprocedure('aios.reconcile_character_belief_atom_authority_v3(uuid,uuid)'))"""
        )
        family_body = await conn.fetchval(
            """SELECT pg_get_functiondef(
               to_regprocedure('aios.apply_character_belief_policy(uuid,uuid)'))"""
        )
        if (not gate_body
            or "source_section_id" not in gate_body
            or "source_sentence_digest" not in gate_body
            or "source_node_id" not in gate_body
            or not authority_body or not family_body
            or "semantic_acquisition_source_eligible" not in authority_body
            or "semantic_acquisition_source_eligible" not in family_body):
            print("FAIL: strict V4 source identity or effective belief source gate missing.")
            return 14
        guard_count = await conn.fetchval(
            """SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = ANY($1::text[])""",
            ["trg_guard_default_belief_policy", "trg_zzzz_skip_unchanged_semantic_admission",
             "trg_refresh_admission_integrity_sentence_edit"],
        )
        if guard_count != 3:
            print(f"FAIL: protected policy/source invalidation triggers incomplete ({guard_count}/3).")
            return 15
        print("OK: default belief policy and effective authority+family resolver")
        print("OK: strict V4 source identity, admission and invalidation contracts")
        # Verify and report actual functions on THIS database; loaded Python
        # constants and installed shadow comparator versions are not receipts.
        from aios_app.epistemic.runtime_versions import capture_runtime_manifest
        manifest = await capture_runtime_manifest(conn)
        effective = manifest["effective_authority"]
        if (effective["belief_executor"] == "unverified"
                or effective["source_integrity_contract"] == "unverified"):
            print(f"FAIL: effective runtime authority could not be verified: {effective['checks']}")
            return 18
        print("OK: effective belief authority:", effective["belief_executor"])
        print("OK: current integrity contract:", effective["source_integrity_contract"])
        print("Effective SQL fingerprints:", effective["sql_fingerprints"])
        print("Complete migration chain SHA-256:", manifest["migration_chain_sha256"])
        print("Migration receipt hashes:", manifest["migration_receipts"])

        hud_receipt = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM aios.schema_migration
                WHERE migration_name=$1
            )
            """,
            HUD_DEFAULT_PROFILE_MIGRATION,
        )
        if not hud_receipt:
            print(f"FAIL: required HUD migration is missing: {HUD_DEFAULT_PROFILE_MIGRATION}")
            print("Run: python -m aios_app.migrate")
            return 8

        default_hud = await conn.fetchrow(
            """
            SELECT profile_id, profile_name
            FROM aios.hud_profile
            WHERE profile_name='default'
            """
        )
        if default_hud is None:
            print("FAIL: required HUD default profile is missing.")
            print("Run: python -m aios_app.migrate")
            return 9
        print(f"OK: HUD default profile {default_hud['profile_id']}")

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
