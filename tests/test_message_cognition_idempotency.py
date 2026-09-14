from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "epistemic" / "message_cognition.py"


def source_text() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_cognition_commit_serializes_same_node_rebuilds():
    source = source_text()
    assert "pg_advisory_lock" in source
    assert "pg_advisory_unlock" in source
    assert "message-cognition:{instance_id}:{node_id}" in source


def test_cognition_unit_write_is_idempotent_without_delete_reinsert_window():
    source = source_text()
    assert "ON CONFLICT (commit_id, ordinal) DO UPDATE" in source
    assert 'DELETE FROM aios.message_cognitive_unit WHERE commit_id=$1' not in source


def test_recompute_retires_surplus_tail_units():
    source = source_text()
    assert "ordinal >= $2" in source
    assert "retired_by_recompute" in source
