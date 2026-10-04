"""Loaded modules are not proof of live SQL authority or comparator execution."""
import asyncio
from aios_app.epistemic.runtime_versions import (
    component_versions, capture_runtime_manifest, INTEGRATION_MIGRATIONS,
    BELIEF_EXECUTOR_VERSION, SOURCE_CONTRACT_VERSION,
)
from aios_app.epistemic import message_cognition, semantic_integrity
from aios_app.agent import participation


class RuntimeDB:
    def __init__(self, *, live=True):
        self.live=live

    async def fetchrow(self, sql, *args):
        if not self.live:
            return {
                "belief_wrapper": "legacy isolated resolver",
                "authority_base": "", "family_materializer": "",
                "integrity_gate": "", "acquisition_gate": "",
                "configured_belief_version": None, "family_count": 0,
            }
        return {
            "belief_wrapper": (
                "PERFORM aios.reconcile_character_belief_atom_authority_v3(); "
                "PERFORM aios.apply_character_belief_policy();"
            ),
            "authority_base": "SELECT aios.semantic_acquisition_source_eligible(kae.acquisition_id);",
            "family_materializer": "SELECT aios.semantic_acquisition_source_eligible(kae.acquisition_id);",
            "integrity_gate": (
                "semantic-integrity-v4-source-coverage source_section_id "
                "source_section_digest source_sentence_digest source_node_id"
            ),
            "acquisition_gate": "semantic_integrity_claim_current",
            "configured_belief_version": BELIEF_EXECUTOR_VERSION,
            "family_count": 16,
        }

    async def fetch(self, sql, *args):
        if not self.live:
            return []
        return [{"migration_name": name, "sha256": "test-" + name}
                for name in ("0001_aios_baseline", *INTEGRATION_MIGRATIONS)]


def test_component_manifest_is_installed_only():
    versions=component_versions()
    assert versions["message_cognition"] == message_cognition.INTERPRETER_VERSION
    assert versions["semantic_integrity"] == semantic_integrity.INTEGRITY_VERSION
    assert versions["participation_v3"] == participation.V3_VERSION
    assert versions["participation_v4"] == participation.V4_VERSION
    assert versions["manifest_scope"] == "installed_modules_not_effective_database"
    assert "belief_materializer" not in versions  # never claim effective SQL from Python
    assert versions["belief_materializer_installed"] == BELIEF_EXECUTOR_VERSION
    assert versions["participation_live_admission"] == "none"


def test_persisted_manifest_captures_effective_sql_and_receipt_versions():
    manifest=asyncio.run(capture_runtime_manifest(
        RuntimeDB(),receipt_versions={"source_integrity":"semantic-integrity-v4-source-coverage"},
    ))
    assert manifest["effective_authority"]["belief_executor"] == BELIEF_EXECUTOR_VERSION
    assert manifest["effective_authority"]["source_integrity_contract"] == SOURCE_CONTRACT_VERSION
    assert manifest["effective_authority"]["sql_fingerprints"]["belief_wrapper_sha256"]
    assert manifest["receipt_versions"]["source_integrity"] == semantic_integrity.INTEGRITY_VERSION
    assert manifest["shadow_comparators"]["participation_v4"] == "comparison-only"
    assert all(k in manifest["migration_receipts"] for k in INTEGRATION_MIGRATIONS)


def test_missing_sql_or_receipts_fails_closed_without_rewriting_installed_metadata():
    manifest=asyncio.run(capture_runtime_manifest(RuntimeDB(live=False)))
    assert manifest["installed_components"]["belief_materializer_installed"] == BELIEF_EXECUTOR_VERSION
    assert manifest["effective_authority"]["belief_executor"] == "unverified"
    assert manifest["effective_authority"]["source_integrity_contract"] == "unverified"
    assert manifest["effective_authority"]["checks"]["required_migrations_applied"] is False


def test_runtime_snapshots_are_persisted_at_the_write_connection():
    import inspect
    from aios_app.agent.participation import ParticipationService
    assert "capture_runtime_manifest(con" in inspect.getsource(ParticipationService.start)
    assert "capture_runtime_manifest(" in inspect.getsource(ParticipationService.process_pending)
    assert "await _effective_runtime_versions(con)" in inspect.getsource(
        message_cognition._commit_message_cognition_locked
    )


def test_effective_runtime_endpoint_is_read_only_and_never_invents_authority():
    import inspect
    from aios_app.agent.api import install_external_agency_routes
    source = inspect.getsource(install_external_agency_routes)
    assert '/agent/runtime/versions' in source
    assert 'return await capture_runtime_manifest(db)' in source
    assert 'HTTPException(503' in source
