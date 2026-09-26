from __future__ import annotations

from uuid import UUID

import pytest

from aios_app.epistemic.goals import CharacterGoalService, CognitiveGoal, ResolvedGoalSet


INSTANCE_ID = UUID("00000000-0000-0000-0000-000000000001")
GOAL_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeDB:
    def __init__(self):
        self.rows = []
        self.created = []

    async def fetch(self, sql, *args):
        return list(self.rows)

    async def fetchrow(self, sql, *args):
        wanted = args[1].lower() if len(args) > 1 else ""
        for row in self.rows:
            if str(row["goal_text"]).lower() == wanted:
                return {"goal_id": row["goal_id"]}
        return None

    async def execute_returning_row(self, sql, *args):
        text = args[5] if len(args) >= 8 else args[2] if len(args) >= 3 else ""
        row = {
            "goal_id": GOAL_ID,
            "goal_text": text,
            "status": "active",
            "priority": args[6] if len(args) >= 8 else 100,
            "meta": {},
        }
        self.rows.append(row)
        self.created.append(text)
        return row


def test_cognitive_goal_string_surface_is_text():
    goal = CognitiveGoal(GOAL_ID, "Find the exit", priority=20)
    assert str(goal) == "Find the exit"
    assert goal.hud_item()["goal_id"] == str(GOAL_ID)
    assert goal.hud_item()["source"] == "agent_goal"


def test_resolved_goal_set_immediate_is_first_ranked_goal():
    first = CognitiveGoal(GOAL_ID, "Urgent", priority=10)
    second = CognitiveGoal(None, "Later", priority=100)
    assert ResolvedGoalSet((first, second)).immediate is first


@pytest.mark.asyncio
async def test_legacy_goal_import_is_idempotent():
    db = FakeDB()
    service = CharacterGoalService(db)
    await service.resolve_active(INSTANCE_ID, legacy_goals=["Find the exit"])
    await service.resolve_active(INSTANCE_ID, legacy_goals=["Find the exit"])
    assert db.created == ["Find the exit"]


@pytest.mark.asyncio
async def test_legacy_goal_dict_accepts_text_field():
    db = FakeDB()
    service = CharacterGoalService(db)
    goals = await service.resolve_active(
        INSTANCE_ID,
        legacy_goals=[{"text": "Learn game control"}],
    )
    assert [goal.text for goal in goals.active] == ["Learn game control"]
