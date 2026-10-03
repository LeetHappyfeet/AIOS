from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "epistemic" / "message_cognition.py"


def source_text() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_rebuild_uses_transactional_same_node_advisory_lock():
    source = source_text()
    assert "pg_advisory_xact_lock" in source
    assert "message-cognition::{instance_id}::{node_id}" in source
    assert "async with con.transaction():" in source


def test_rebuild_is_atomic_and_idempotent_by_source_and_effective_version():
    source = source_text()
    assert 'existing["source_text_hash"] == digest' in source
    assert 'existing["interpreter_version"] == INTERPRETER_VERSION' in source
    assert 'DELETE FROM aios.message_cognitive_unit WHERE commit_id=$1' in source
    assert 'ON CONFLICT (instance_id, node_id) DO UPDATE' in source


def test_historical_recovery_defers_out_of_order_cognitive_side_effects():
    source = source_text()
    assert '"historical_catchup": not bool(live_head)' in source
    assert '"goal_projection_deferred"' in source
    assert '"enrichment_deferred"' in source
    assert 'if live_head:' in source
    assert 'source_head_node_id=$2' in source
