from __future__ import annotations

from uuid import UUID

import pytest

from aios_app.epistemic.goals import CharacterGoalService, CognitiveGoal, ResolvedGoalSet


@pytest.fixture(autouse=True)
def isolate_scene_refresh(monkeypatch):
    # These are lifecycle unit tests; scene projection has separate coverage.
    async def refresh(self, instance_id):
        pass
    monkeypatch.setattr("aios_app.epistemic.goals.CharacterGoalService._refresh_scene", refresh)


INSTANCE_ID = UUID("00000000-0000-0000-0000-000000000001")
GOAL_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeDB:
    def __init__(self):
        self.rows = []
        self.created = []

    async def fetch(self, sql, *args):
        return list(self.rows)

    async def execute(self, sql, *args):
        return "INSERT 0 0"

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


def test_goal_reconciler_is_wired_to_message_cognition():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "epistemic" / "message_cognition.py").read_text(encoding="utf-8")
    assert 'unit.claim_kind == "GOAL"' in source
    assert 'unit.meta.get("character_owned")' in source
    assert "reconcile_evidence(" in source


def test_persistent_continuity_requires_same_identity_and_excludes_future_updates():
    from inspect import getsource
    source = getsource(CharacterGoalService._carry_persistent)
    assert "source.character_id=t.character_id" in source
    assert "owner_user_id" not in source
    assert "source.meta->>'runtime_user_name'=t.user_name" in source
    assert "g.updated_at<t.created_at" in source
    assert "c.status='active'" in source
    assert "inherited_from_goal_id" in source


def test_goal_formation_runs_on_isolated_inference_job():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    operations=(root / "agent" / "cognitive_operations.py").read_text()
    registry=(root / "pipeline" / "job_registry.py").read_text()
    assert 'job_type="goal_formulation_inference"' in operations
    assert '"goal_formulation_inference": JobSpec(ResourceClass.GLOBAL' in registry
    assert "source['speaker_role'] not in" in operations.replace('"', "'")


def test_goal_backfill_is_bounded_and_character_owned():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "migrations" / "current" / "20260925_01_goal_convergence.sql").read_text(encoding="utf-8")
    assert "character_owned" in source
    assert "recency_rank <= 3" in source
    assert "semantic_topic_key" in source


def test_goal_withdrawal_is_parsed_as_negative_goal_evidence():
    from aios_app.epistemic.message_cognition import interpret_message
    units = interpret_message(
        "I don't want to leave anymore.",
        character_id="Renamon",
        speaker_id="Renamon",
        speaker_role="assistant",
        viewpoint_id="Renamon",
    )
    goals = [unit for unit in units if unit.claim_kind == "GOAL"]
    assert len(goals) == 1
    assert goals[0].polarity == -1


def test_no_longer_goal_is_parsed_as_negative_goal_evidence():
    from aios_app.epistemic.message_cognition import interpret_message
    units = interpret_message(
        "I no longer want to leave.",
        character_id="Renamon",
        speaker_id="Renamon",
        speaker_role="assistant",
        viewpoint_id="Renamon",
    )
    goals = [unit for unit in units if unit.claim_kind == "GOAL"]
    assert len(goals) == 1
    assert goals[0].polarity == -1


def test_narrative_markup_after_to_does_not_become_a_goal():
    from aios_app.epistemic.message_cognition import interpret_message
    units = interpret_message(
        "Renamon wants to*—as though it were that simple.",
        character_id="Renamon", speaker_id="Renamon",
        speaker_role="character", viewpoint_id="Renamon",
    )
    assert not any(unit.claim_kind == "GOAL" for unit in units)


def test_complete_infinitive_remains_a_goal():
    from aios_app.epistemic.message_cognition import interpret_message
    units = interpret_message(
        "I want to visit the bookstore tomorrow.",
        character_id="Renamon", speaker_id="Renamon",
        speaker_role="character", viewpoint_id="Renamon",
    )
    assert [unit.meta["objective"] for unit in units if unit.claim_kind == "GOAL"] == [
        "to visit the bookstore tomorrow"
    ]


class LifecycleDB:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetch(self, sql, *args):
        if "character_goal_evidence" in sql and "GROUP BY" in sql:
            return [{"goal_id": GOAL_ID, "relation": "progress", "count": 2}]
        if "character_cognitive_thread" in sql:
            return [{"goal_id": GOAL_ID, "status": "working", "pressure": 3,
                     "crossing_count": 4, "current_question": "What next?", "updated_at": None}]
        if "DISTINCT ON (goal_id)" in sql:
            return [{"goal_id": GOAL_ID, "relation": "progress",
                     "evidence_type": "cognitive_operation", "confidence": .65, "meta": {}}]
        return []


@pytest.mark.asyncio
async def test_goal_cognitive_state_projects_thread_and_evidence():
    db = LifecycleDB()
    service = CharacterGoalService(db)
    states = await service.cognitive_states(
        INSTANCE_ID, [CognitiveGoal(GOAL_ID, "Find the exit")]
    )
    state = states[GOAL_ID]
    assert state["progress_count"] == 2
    assert state["thread_status"] == "working"
    assert state["pressure"] == 3
    assert state["crossing_count"] == 4
    assert state["latest_evidence"]["relation"] == "progress"


@pytest.mark.asyncio
async def test_goal_invalidation_advances_runtime_and_dirties_hud():
    db = LifecycleDB()
    service = CharacterGoalService(db)
    await service._invalidate(INSTANCE_ID)
    sql = "\n".join(statement for statement, _ in db.executed)
    assert "state_version=state_version+1" in sql
    assert "character_hud_readiness" in sql
    assert "status='dirty'" in sql


def test_goal_completion_uses_goal_service_not_direct_status_update():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "agent" / "cognitive_lifecycle.py").read_text(encoding="utf-8")
    block = source[source.index("async def complete_goal"):source.index("async def reconcile_goal_threads")]
    assert "self.goals.finish(" in block
    assert "SET status='completed'" not in block


def test_planning_hud_consumes_goal_lifecycle_projection():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "hud" / "internal_frame.py").read_text(encoding="utf-8")
    assert 'worker_profile == "planning"' in source
    assert '"GOAL STATE:"' in source
    assert 'lifecycle.get("crossing_count")' in source
