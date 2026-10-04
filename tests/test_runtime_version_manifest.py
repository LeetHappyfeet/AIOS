"""Runtime attribution is taken from loaded worker modules, not branch labels."""
from aios_app.epistemic.runtime_versions import component_versions
from aios_app.epistemic import message_cognition, semantic_integrity
from aios_app.agent import participation


def test_component_version_manifest_reflects_loaded_code():
    versions = component_versions()
    assert versions["message_cognition"] == message_cognition.INTERPRETER_VERSION
    assert versions["semantic_integrity"] == semantic_integrity.INTEGRITY_VERSION
    assert versions["participation_v3"] == participation.V3_VERSION
    assert versions["participation_v4"] == participation.V4_VERSION
    assert versions["message_cognition"] == "message-cognition-v11-source-goal-admission+epistemic-scope-v1"
    assert message_cognition.BASE_INTERPRETER_VERSION == "message-cognition-v11-source-goal-admission"
    assert message_cognition.SCOPE_POLICY_VERSION == "epistemic-scope-v1"
    assert versions["semantic_integrity"] == "semantic-integrity-v4-source-coverage"


def test_experiment_and_cognition_persist_runtime_provenance():
    import inspect
    participation_source = inspect.getsource(participation.ParticipationService)
    cognition_source = inspect.getsource(message_cognition._commit_message_cognition_locked)
    assert "runtime_versions" in participation_source
    assert "comparison_v3" in participation_source
    assert "runtime_versions" in cognition_source


def test_manifest_separates_effective_execution_from_shadow_comparators():
    versions = component_versions()
    assert versions["belief_materializer"] == "character-belief-v4-authority-family"
    assert versions["belief_authority_base"] == "character-belief-v3-authority-lineage"
    assert versions["belief_family_policy"] == "semantic-policy-v1"
    assert versions["source_integrity_contract"] == "integrity-contract-v1"
    assert versions["participation_live_admission"] == "none"
    assert versions["participation_v4_execution"] == "shadow-comparison-only"
    assert versions["participation_primary_execution"] == "shadow-v1-ledger-only"
