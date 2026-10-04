"""Separate installed executors, verified database authority, and receipt provenance.

component_versions() advertises locally installed code ONLY. For claims about
active PostgreSQL authority, call capture_runtime_manifest() against the same
connection/transaction that will persist the event or evaluation.
"""
from __future__ import annotations

import hashlib
from typing import Any

MANIFEST_VERSION = "aios-effective-runtime-manifest-v2"
BELIEF_EXECUTOR_VERSION = "character-belief-v4-authority-family"
BELIEF_BASE_VERSION = "character-belief-v3-authority-lineage"
FAMILY_POLICY_VERSION = "semantic-policy-v1"
SOURCE_CONTRACT_VERSION = "integrity-contract-v2-source-coordinate"
REQUIRED_FAMILY_COUNT = 16
INTEGRATION_MIGRATIONS = (
    "20261003_10_integrated_belief_policy_and_integrity.sql",
    "20261003_11_integrity_context_invalidation.sql",
    "20261003_12_occurrence_completion_invalidation.sql",
    "20261003_13_source_receipt_and_belief_hardening.sql",
    "20261003_14_participation_worker_heartbeat.sql",
)


def component_versions() -> dict[str, str]:
    """Installed module constants are *not* database execution receipts."""
    from aios_app.epistemic import (
        semantic_frames, context_resolver, semantic_integrity, normalizer,
        message_cognition,
    )
    from aios_app.agent import participation
    return {
        "semantic_frames": semantic_frames.DECOMPOSER_VERSION,
        "context_resolver": context_resolver.RESOLVER_VERSION,
        "semantic_integrity": semantic_integrity.INTEGRITY_VERSION,
        "normalizer": normalizer.NORMALIZER_VERSION,
        "message_cognition": message_cognition.INTERPRETER_VERSION,
        "participation_primary": participation.POLICY_VERSION,
        "participation_v2": participation.COMPARISON_VERSION,
        "participation_v3": participation.V3_VERSION,
        "participation_v4": participation.V4_VERSION,
        "participation_primary_execution": "shadow-v1-ledger-only",
        "participation_v2_execution": "shadow-comparison-only",
        "participation_v3_execution": "shadow-comparison-only",
        "participation_v4_execution": "shadow-comparison-only",
        "participation_live_admission": "none",
        "belief_materializer_installed": BELIEF_EXECUTOR_VERSION,
        "belief_authority_base_installed": BELIEF_BASE_VERSION,
        "belief_family_policy_installed": FAMILY_POLICY_VERSION,
        "source_integrity_contract_installed": SOURCE_CONTRACT_VERSION,
        "manifest_scope": "installed_modules_not_effective_database",
    }


def _fingerprint(definition: str | None) -> str | None:
    return hashlib.sha256(definition.encode("utf-8")).hexdigest() if definition else None


async def capture_runtime_manifest(db: Any, *, receipt_versions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the *effective* SQL executors, policies and ledger on this DB.

    No write, no inference and no assumption that installing V4 enabled it.
    A missing function, reference row or migration is reported as unverified.
    Caller may use an asyncpg connection or the standard AIOS database wrapper.
    """
    row = await db.fetchrow(
        """SELECT
             pg_get_functiondef(to_regprocedure(
               'aios.reconcile_character_belief_atom(uuid,uuid)')) AS belief_wrapper,
             pg_get_functiondef(to_regprocedure(
               'aios.reconcile_character_belief_atom_authority_v3(uuid,uuid)')) AS authority_base,
             pg_get_functiondef(to_regprocedure(
               'aios.apply_character_belief_policy(uuid,uuid)')) AS family_materializer,
             pg_get_functiondef(to_regprocedure(
               'aios.semantic_integrity_claim_current(uuid)')) AS integrity_gate,
             pg_get_functiondef(to_regprocedure(
               'aios.semantic_acquisition_source_eligible(uuid)')) AS acquisition_gate,
             (SELECT resolver_version FROM aios.belief_reconciliation_policy
               WHERE policy_key='default') AS configured_belief_version,
             (SELECT count(*) FROM aios.reconciliation_family_policy)
                AS family_count"""
    )
    data = dict(row) if row else {}
    migration_rows = await db.fetch(
        """SELECT migration_name, sha256 FROM aios.schema_migration
           ORDER BY migration_name"""
    )
    migrations = {r["migration_name"]: r["sha256"] for r in migration_rows}
    wrapper = data.get("belief_wrapper") or ""
    authority = data.get("authority_base") or ""
    family = data.get("family_materializer") or ""
    integrity = data.get("integrity_gate") or ""
    acquisition = data.get("acquisition_gate") or ""
    integrated = (
        "reconcile_character_belief_atom_authority_v3" in wrapper
        and "apply_character_belief_policy" in wrapper
        and "semantic_acquisition_source_eligible" in authority
        and "semantic_acquisition_source_eligible" in family
        and data.get("configured_belief_version") == BELIEF_EXECUTOR_VERSION
        and int(data.get("family_count") or 0) == REQUIRED_FAMILY_COUNT
    )
    v4_current = all(s in integrity for s in (
        "semantic-integrity-v4-source-coverage", "source_section_id",
        "source_section_digest", "source_sentence_digest", "source_node_id",
    )) and "semantic_integrity_claim_current" in acquisition
    required_present = (
        "0001_aios_baseline" in migrations
        and all(key in migrations for key in INTEGRATION_MIGRATIONS)
    )
    chain_identity = hashlib.sha256(
        "\\n".join(f"{name}:{digest}" for name, digest in sorted(migrations.items())).encode("utf-8")
    ).hexdigest()
    return {
        "manifest_version": MANIFEST_VERSION,
        "installed_components": component_versions(),
        "effective_authority": {
            "belief_executor": (
                BELIEF_EXECUTOR_VERSION if integrated and required_present
                else "unverified"
            ),
            "belief_authority_base": (
                BELIEF_BASE_VERSION if integrated and required_present else "unverified"
            ),
            "belief_family_policy": (
                FAMILY_POLICY_VERSION if integrated and required_present else "unverified"
            ),
            "source_integrity_contract": (
                SOURCE_CONTRACT_VERSION if v4_current and required_present
                else "unverified"
            ),
            "participation_live_admission": "none",
            "checks": {
                "authority_family_composed": integrated,
                "current_v4_source_gate": v4_current,
                "required_migrations_applied": required_present,
                "family_count": int(data.get("family_count") or 0),
                "default_policy_version": data.get("configured_belief_version"),
            },
            "sql_fingerprints": {
                "belief_wrapper_sha256": _fingerprint(wrapper),
                "authority_base_sha256": _fingerprint(authority),
                "family_materializer_sha256": _fingerprint(family),
                "integrity_gate_sha256": _fingerprint(integrity),
                "acquisition_gate_sha256": _fingerprint(acquisition),
            },
        },
        "shadow_comparators": {
            "participation_v1": "ledger-only",
            "participation_v2": "comparison-only",
            "participation_v3": "comparison-only",
            "participation_v4": "comparison-only",
        },
        "migration_receipts": migrations,
        "migration_chain_sha256": chain_identity,
        "receipt_versions": dict(receipt_versions or {}),
    }
