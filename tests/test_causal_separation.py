from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_has_no_second_location_mutation_path():
    source = (ROOT / "world" / "runtime.py").read_text(encoding="utf-8")

    assert "SET location_entity_id" not in source
    assert "UPDATE aios.character_runtime_state SET location_entity_id" not in source
    assert "await self._validate_action_rules" not in source
    assert "def _validate_action_rules" not in source
    assert "self.causal.require_commit" in source


def test_hud_context_never_reads_hidden_causal_state_or_legacy_location_column():
    source = (ROOT / "hud" / "context.py").read_text(encoding="utf-8")

    assert "FROM aios.causal_state" not in source
    assert "rs.location_entity_id" not in source
    assert "relation_type='located_in'" in source


def test_causal_kernel_does_not_write_character_epistemic_tables():
    source = (ROOT / "causal" / "kernel.py").read_text(encoding="utf-8")

    forbidden = (
        "character_knowledge",
        "character_proposition_knowledge",
        "knowledge_acquisition_event",
        "character_relationship",
    )
    for table in forbidden:
        assert table not in source


def test_causal_projection_is_world_only():
    source = (ROOT / "causal" / "projection.py").read_text(encoding="utf-8")

    assert "character_knowledge" not in source
    assert "character_proposition_knowledge" not in source
    assert "knowledge_acquisition_event" not in source
