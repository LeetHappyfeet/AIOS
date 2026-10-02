"""Runtime attribution is taken from loaded worker modules, not branch labels."""
from aios_app.epistemic.runtime_versions import component_versions
from aios_app.epistemic import message_cognition, semantic_integrity
from aios_app.agent import participation


def test_component_version_manifest_reflects_loaded_code():
    versions = component_versions()
    assert versions["message_cognition"] == message_cognition.INTERPRETER_VERSION
    assert versions["semantic_integrity"] == semantic_integrity.INTEGRITY_VERSION
    assert versions["participation_v3"] == participation.V3_VERSION
    assert versions["message_cognition"] == "message-cognition-v8-source-owned"
    assert versions["semantic_integrity"] == "semantic-integrity-v3-fidelity"


def test_experiment_and_cognition_persist_runtime_provenance():
    import inspect
    participation_source = inspect.getsource(participation.ParticipationService)
    cognition_source = inspect.getsource(message_cognition._commit_message_cognition_locked)
    assert "runtime_versions" in participation_source
    assert "comparison_v3" in participation_source
    assert "runtime_versions" in cognition_source
