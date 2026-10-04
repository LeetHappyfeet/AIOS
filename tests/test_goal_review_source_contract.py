"""Require bounded and source-fresh goal-review evidence."""
import inspect

from aios_app.agent.cognitive_operations import CognitiveOperationEngine
from aios_app.agent.opportunities import CharacterOpportunityGenerator


def test_goal_review_receives_source_window_before_inference():
    # The source window comes from the already branch-scoped attention source.
    # Keep this structural test alongside the lifecycle's fake-DB negative tests.
    source = inspect.getsource(CharacterOpportunityGenerator.generate)
    queue = inspect.getsource(CognitiveOperationEngine._queue_decision)
    assert 'row.get("event_stream")=="source"' in source
    assert '][:4]' in source
    assert '"recent_source_events":recent_source_events' in source
    assert 'recent_source_events' in queue
    assert 'Recent source turns (newest first)' in queue


def test_planning_review_is_strict_at_both_generation_and_acceptance():
    generator = inspect.getsource(CharacterOpportunityGenerator.generate)
    queue = inspect.getsource(CognitiveOperationEngine._queue_decision)
    accept = inspect.getsource(CognitiveOperationEngine.accept_choice)
    assert 'freshness="strict"' in generator
    assert '{"executive.review","planning.review"}' in queue
    assert '{"executive.review","planning.review"}' in accept
