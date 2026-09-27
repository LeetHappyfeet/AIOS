from uuid import uuid4

import pytest

from aios_app.agent.cognitive_subjects import (
    CognitiveSubject,
    GoalKnowledgeDemandResolver,
)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


def subject():
    return CognitiveSubject(
        subject_id=uuid4(), canonical_key="goal:test", subject_type="goal_knowledge",
        entity_keys=("photography",), predicate_key=None, object_key=None,
        topic_key="photography", question_type="knowledge_demand",
        question="What knowledge would help satisfy this goal?",
        display_label="Learn photography", confidence=1.0, uncertainty=0.0, salience=.8,
    )


@pytest.mark.asyncio
async def test_goal_demand_uses_character_topology_before_lexical_fallback():
    rows = [
        {"topology_node_id": uuid4(), "node_type": "PROPOSITION",
         "node_key": "aperture", "label": "Aperture controls light",
         "proposition_id": uuid4(), "significance": .9},
        {"topology_node_id": uuid4(), "node_type": "PROPOSITION",
         "node_key": "shutter-speed", "label": "Shutter speed controls exposure time",
         "proposition_id": uuid4(), "significance": .8},
    ]
    result = await GoalKnowledgeDemandResolver(FakeDB(rows)).resolve(
        instance_id=uuid4(), subject=subject(), known=[])
    assert result["coverage_source"] == "character_topology"
    assert result["internal_coverage"] >= .67
    assert result["next_source"] == "none"


@pytest.mark.asyncio
async def test_goal_demand_falls_back_to_existing_subject_coverage_without_topology():
    result = await GoalKnowledgeDemandResolver(FakeDB([])).resolve(
        instance_id=uuid4(), subject=subject(), known=[])
    assert result["coverage_source"] == "lexical_fallback"
    assert result["next_source"] == "corpus"
