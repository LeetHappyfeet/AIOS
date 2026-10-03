"""Experiment 5 regression: skipped DAG turns must get cognition receipts.

No postgres connection is required for these unit tests. SQL scope and cursor
safety have additional integration checks in the deployment guide.
"""
import asyncio
from uuid import uuid4

import pytest

from aios_app.epistemic import cognition_catchup, message_cognition
from aios_app.epistemic.epistemic_scope import install_message_cognition_scope_guard


def test_effective_version_is_base_plus_scope_not_legacy_v4():
    install_message_cognition_scope_guard(message_cognition)
    assert message_cognition.BASE_INTERPRETER_VERSION == "message-cognition-v8-source-owned"
    assert message_cognition.SCOPE_POLICY_VERSION == "epistemic-scope-v1"
    assert message_cognition.INTERPRETER_VERSION == (
        "message-cognition-v8-source-owned+epistemic-scope-v1"
    )


def test_george_discourse_marker_is_not_renamon_belief():
    install_message_cognition_scope_guard(message_cognition)
    for source in (
        "You know what, that's fair.",
        "You know what, bring two cups.",
        "You know what, that's actually more than my father ever gives me.",
    ):
        units = message_cognition.interpret_message(
            source, character_id="Renamon", speaker_id="George Constanza",
            speaker_role="user", viewpoint_id="Renamon",
        )
        assert not any(u.claim_kind == "BELIEF" and u.meta["character_owned"] for u in units)


def test_external_you_assertion_does_not_become_character_owned():
    units = message_cognition.interpret_message(
        "You are digital now.", character_id="Renamon", speaker_id="George Constanza",
        speaker_role="user", viewpoint_id="George Constanza",
    )
    assert not any(u.meta["character_owned"] for u in units)


def test_self_authored_positive_goal_not_suppressed():
    units = message_cognition.interpret_message(
        "I want to sleep on the couch.", character_id="Renamon",
        speaker_id="Renamon", speaker_role="character", viewpoint_id="Renamon",
    )
    assert any(u.claim_kind == "GOAL" and u.polarity == 1
               and u.meta["character_owned"] for u in units)


def test_gap_scan_is_ancestry_and_perception_scoped():
    class QueryDB:
        query = ""
        args = None

        async def fetch(self, query, *args):
            self.query, self.args = query, args
            return []

    db = QueryDB()
    instance_id = uuid4()
    assert asyncio.run(cognition_catchup.missing_cognition_nodes(
        db, instance_id=instance_id, limit=7,
    )) == []
    assert db.args == (instance_id, 7)
    assert "WITH RECURSIVE source_chain" in db.query
    assert "edge.parent_node_id" in db.query
    assert "chain.visited" in db.query
    assert "mp.perceived" in db.query
    assert "c.instance_id=rs.instance_id" in db.query
    assert "NOT EXISTS" in db.query
    assert "ORDER BY dn.event_id ASC" in db.query


@pytest.mark.parametrize("limit", [0, -1, 33, 100])
def test_scan_rejects_unbounded_batch(limit):
    class NeverDB:
        async def fetch(self, *_args):
            raise AssertionError("Out-of-range query should not execute")

    with pytest.raises(ValueError):
        asyncio.run(cognition_catchup.missing_cognition_nodes(
            NeverDB(), instance_id=uuid4(), limit=limit,
        ))


def test_recovery_processes_missing_events_in_source_order(monkeypatch):
    instance = uuid4()
    nodes = [{"node_id": uuid4(), "event_id": event_id}
             for event_id in (9, 10, 11, 12, 13, 14)]
    called = []

    async def fake_scan(_db, *, instance_id, limit):
        assert instance_id == instance
        assert limit == 32
        return nodes

    async def fake_commit(_db, *, instance_id, node_id):
        called.append(node_id)
        return True

    monkeypatch.setattr(cognition_catchup, "missing_cognition_nodes", fake_scan)
    monkeypatch.setattr(cognition_catchup, "commit_message_cognition", fake_commit)
    result = asyncio.run(cognition_catchup.recover_missing_cognition(
        object(), instance_id=instance,
    ))
    assert called == [r["node_id"] for r in nodes]
    assert result == {"scanned": 6, "committed": 6, "failed": 0, "more_possible": False}


def test_committed_zero_unit_node_is_not_a_gap():
    # The scan excludes nodes by message_cognitive_commit existence, NOT by
    # nonzero unit count. Keeping empty receipts avoids infinite retries.
    import inspect
    sql = inspect.getsource(cognition_catchup.missing_cognition_nodes)
    assert "message_cognitive_commit c" in sql
    assert "message_cognitive_unit" not in sql


def test_historical_recovery_cannot_move_current_cursor():
    import inspect
    from aios_app.epistemic.message_cognition import (
        _advance_cognitive_cursor_on_connection, _commit_message_cognition_locked,
    )
    cursor = inspect.getsource(_advance_cognitive_cursor_on_connection)
    commit = inspect.getsource(_commit_message_cognition_locked)
    assert "source_head_node_id=$2" in cursor
    assert "cognitive_ready_event_id <= $3" in cursor
    assert "historical_catchup" in commit
    assert "if live_head:" in commit
