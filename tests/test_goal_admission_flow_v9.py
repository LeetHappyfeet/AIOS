"""Owned intention -> managed goal -> participation -> HUD attention (generic fixture)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from aios_app.agent.participation import ParticipationService
from aios_app.epistemic import message_cognition as cognition
from aios_app.epistemic.cognitive_context import CognitiveContextService
from aios_app.epistemic.goals import CharacterGoalService
from aios_app.epistemic.epistemic_scope import install_message_cognition_scope_guard


CHARACTER = "Character_A"


class FlowDB:
    def __init__(self):
        self.rows = []
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if "FROM aios.character_agent_goal" in sql:
            rows = self.rows
            if "meta->>'semantic_topic_key'" in sql:
                rows = [r for r in rows if r["meta"].get("semantic_topic_key") == args[1]]
            if "status='active'" in sql:
                rows = [r for r in rows if r["status"] == "active"]
            return list(rows)
        return []

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        if "source_head_node_id" in sql:
            return args[0] if len(args) > 1 else None
        return None

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "UPDATE 1"

    async def execute_returning_row(self, sql, *args):
        self.calls.append(("execute_returning_row", sql, args))
        if "UPDATE aios.character_agent_goal" in sql:
            row = next(r for r in self.rows if r["goal_id"] == args[0])
            row["goal_text"] = args[2]
            row["source_node_id"] = args[3]
            row["meta"].update(json.loads(args[4]))
            return row
        assert "INSERT INTO aios.character_agent_goal" in sql
        row = {
            "goal_id": uuid4(),
            "goal_text": args[5],
            "status": "active",
            "priority": args[6],
            "meta": json.loads(args[7]),
            "source_node_id": args[2],
            "updated_at": datetime.now(timezone.utc),
        }
        self.rows.append(row)
        return row


async def _flow():
    install_message_cognition_scope_guard(cognition)
    db = FlowDB()
    instance, source = uuid4(), uuid4()
    units = cognition.interpret_message(
        "I will repair the receiver.", character_id=CHARACTER,
        speaker_id=CHARACTER, speaker_role="character", viewpoint_id=CHARACTER,
    )
    goal_units = [u for u in units if u.claim_kind == "GOAL" and
                  u.meta["character_owned"] and u.polarity > 0]
    assert len(goal_units) == 1
    unit = goal_units[0]
    service = CharacterGoalService(db)
    created = await service.reconcile_evidence(
        instance_id=instance, text=unit.text, topic_key=unit.topic_key,
        polarity=unit.polarity, source_node_id=source, source_unit_id=uuid4(),
        confidence=unit.confidence, salience=unit.salience,
        intent_type=unit.meta["intent_type"], horizon=unit.meta["horizon"],
        objective=unit.meta["objective"], refresh_scene=False,
    )
    assert created is not None and created.goal_id is not None
    assert len(db.rows) == 1

    # The participation snapshot consumes managed active goals, not a separate
    # reconstructed proposition-based intention list.
    participant = ParticipationService(db)
    participation = await participant._context(db, {
        "instance_id": instance, "character_id": CHARACTER,
        "display_name": "Character A", "canonical_name": "Character A",
    })
    assert len(participation["goals"]) == 1
    assert participation["goals"][0]["goal_id"] == created.goal_id

    # This is the real attention-input resolver used by HUDAssembler.build.
    # No historical semantic GOAL is promoted merely by retrieval.
    cognitive = CognitiveContextService(db)
    context = SimpleNamespace(
        instance_id=instance, timeline_id=uuid4(), source_timeline_id=None,
        source_head_node_id=None, character_id=CHARACTER,
    )
    attention = await cognitive.resolve_attention_inputs(
        context, {}, {}, recent_limit=2, focus_text="Current intention",
    )
    assert len(attention.goals) == 1
    assert attention.goals[0].goal_id == created.goal_id
    hud_item = attention.goals[0].hud_item()
    assert hud_item["source"] == "agent_goal"
    assert hud_item["text"] == created.text
    assert hud_item["status"] == "active"

    # Reconciliation of the same evidence must update, not duplicate, the goal.
    again = await service.reconcile_evidence(
        instance_id=instance, text=unit.text, topic_key=unit.topic_key,
        polarity=1, source_node_id=source, objective=unit.meta["objective"],
        refresh_scene=False,
    )
    assert again is not None and again.goal_id == created.goal_id
    assert len(db.rows) == 1


def test_owned_positive_goal_reaches_participation_and_hud_attention(monkeypatch):
    async def no_scene(self, instance_id, source_node_id):
        return {}

    monkeypatch.setattr(CharacterGoalService, "_origin_scene", no_scene)
    asyncio.run(_flow())
