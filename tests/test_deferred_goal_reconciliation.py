"""Generic, source-ordered historical goal projection regressions."""
import asyncio
import inspect
from uuid import uuid4

from aios_app.epistemic.deferred_goal_reconciliation import reconcile_deferred_goals


TOPIC = "goal:character:repair:receiver"


def make_unit(event, ordinal=0, polarity=1, horizon="session"):
    return {
        "unit_id": uuid4(), "commit_id": uuid4(), "node_id": uuid4(),
        "event_id": event, "ordinal": ordinal, "text": "Character A intends repair the receiver.",
        "topic_key": TOPIC, "polarity": polarity, "confidence": .9, "salience": .8,
        "meta": {"character_owned": True, "objective": "repair the receiver",
                 "horizon": horizon, "intent_type": "commitment"},
        "status": "active",
        "summary": {"historical_catchup": True, "goal_projection_deferred": True},
    }


class FakeDB:
    def __init__(self, units, existing=()):
        self.units = units
        self.existing = list(existing)
        self.source_head = uuid4()
        self.timeline = uuid4()
        self.executed = []

    def connection(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        if "FROM aios.character_runtime_state rs" in query:
            return {"source_timeline_id": self.timeline,
                    "source_head_node_id": self.source_head}
        raise AssertionError("Unexpected fetchrow: " + query)

    async def fetch(self, query, *args):
        if "WITH RECURSIVE chain" in query:
            return [{"node_id": self.source_head}] + [
                {"node_id": u["node_id"]} for u in self.units]
        if "FROM aios.message_cognitive_unit u" in query:
            return self.units
        if "FROM aios.character_agent_goal g" in query:
            return self.existing
        raise AssertionError("Unexpected fetch: " + query)


def test_later_withdrawal_supersedes_older_positive(monkeypatch):
    from aios_app.epistemic.goals import CharacterGoalService
    calls = []

    async def goal_reconcile(self, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(CharacterGoalService, "reconcile_evidence", goal_reconcile)
    earlier, later = make_unit(10), make_unit(12, polarity=-1)
    db = FakeDB([earlier, later])
    result = asyncio.run(reconcile_deferred_goals(db, instance_id=uuid4()))
    assert result["considered"] == 2
    assert result["superseded"] == 1
    assert result["applied"] == 0
    assert len(calls) == 1 and calls[0]["polarity"] == -1
    statuses = [args[1] for sql, args in db.executed
                if "historical_goal_reconciliation" in sql]
    assert statuses == ["superseded_by_later_source_evidence",
                        "negative_without_active_goal"]


def test_later_authoritative_goal_is_never_overwritten(monkeypatch):
    from aios_app.epistemic.goals import CharacterGoalService

    async def forbid(self, **_kwargs):
        raise AssertionError("Older goal must not reach the authoritative writer")

    monkeypatch.setattr(CharacterGoalService, "reconcile_evidence", forbid)
    db = FakeDB(
        [make_unit(10)],
        [{"source_event_id": 25, "meta": {"source": "message_cognition"},
          "status": "active", "goal_id": uuid4()}],
    )
    result = asyncio.run(reconcile_deferred_goals(db, instance_id=uuid4()))
    assert result["protected"] == 1 and result["applied"] == 0


def test_immediate_historical_action_is_not_managed_as_future_goal(monkeypatch):
    from aios_app.epistemic.goals import CharacterGoalService

    async def forbid(self, **_kwargs):
        raise AssertionError("Immediate action must remain outside managed goals")

    monkeypatch.setattr(CharacterGoalService, "reconcile_evidence", forbid)
    db = FakeDB([make_unit(10, horizon="immediate")])
    result = asyncio.run(reconcile_deferred_goals(db, instance_id=uuid4()))
    assert result["rejected"] == 1
    assert any(
        args[1] == "immediate_action_not_managed_goal"
        for sql, args in db.executed if "historical_goal_reconciliation" in sql
    )


def test_pipeline_holds_replay_until_source_and_inference_complete():
    from aios_app.runner_v2 import handle_message_cognition_catchup
    code = inspect.getsource(handle_message_cognition_catchup)
    assert "missing_cognition_nodes" in code
    assert "deferred_enrichment_nodes" in code
    assert code.index("if await deferred_enrichment_nodes") < code.index(
        "reconcile_deferred_goals")
