from uuid import uuid4

from aios_app.epistemic.episode_projection import project_semantic_episode
from aios_app.epistemic.retrieval import TopologyRetriever
from aios_app.pipeline.job_registry import ResourceClass, job_spec


def test_episode_projection_orders_complete_events():
    episode_id = uuid4()
    event_a = uuid4()
    event_b = uuid4()
    projection = project_semantic_episode(
        {"semantic_episode_id": episode_id},
        [
            {
                "semantic_event_id": event_b,
                "episode_ordinal": 1,
                "members": [{
                    "proposition_id": uuid4(),
                    "subject_norm": "alex",
                    "predicate_norm": "reach",
                    "object_norm": "the pad",
                    "text": "alex | reach | the pad",
                }],
            },
            {
                "semantic_event_id": event_a,
                "episode_ordinal": 0,
                "members": [{
                    "proposition_id": uuid4(),
                    "subject_norm": "renamon",
                    "predicate_norm": "stop",
                    "object_norm": "at the side door",
                    "text": "renamon | stop | at the side door",
                }],
            },
        ],
    )
    assert projection["semantic_episode_id"] == episode_id
    assert projection["semantic_event_ids"] == [event_a, event_b]
    assert projection["text"].startswith("renamon stop at the side door.")
    assert "alex reach the pad." in projection["text"]


def test_episode_collapse_prefers_episode_identity_over_atomic_events():
    episode_id = uuid4()
    event_a = uuid4()
    event_b = uuid4()
    prop_a = uuid4()
    prop_b = uuid4()
    items = [
        {
            "proposition_id": prop_a,
            "semantic_event_id": event_a,
            "semantic_event_confidence": 0.8,
            "text": "renamon stop at the side door.",
            "relevance": {"total": 2.0},
        },
        {
            "proposition_id": prop_b,
            "semantic_event_id": event_b,
            "semantic_event_confidence": 0.8,
            "text": "alex reach the pad.",
            "relevance": {"total": 1.8},
        },
    ]
    episode_events = [
        {
            "semantic_event_id": event_a,
            "episode_ordinal": 0,
            "event_confidence": 0.8,
            "members": [{
                "proposition_id": prop_a,
                "subject_norm": "renamon",
                "predicate_norm": "stop",
                "object_norm": "at the side door",
                "text": "renamon | stop | at the side door",
            }],
        },
        {
            "semantic_event_id": event_b,
            "episode_ordinal": 1,
            "event_confidence": 0.8,
            "members": [{
                "proposition_id": prop_b,
                "subject_norm": "alex",
                "predicate_norm": "reach",
                "object_norm": "the pad",
                "text": "alex | reach | the pad",
            }],
        },
    ]
    mapping = {
        event_a: {
            "semantic_episode_id": episode_id,
            "ordinal": 0,
            "episode_events": episode_events,
        },
        event_b: {
            "semantic_episode_id": episode_id,
            "ordinal": 1,
            "episode_events": episode_events,
        },
    }
    collapsed = TopologyRetriever._collapse_semantic_episodes(items, mapping)
    assert len(collapsed) == 1
    assert collapsed[0]["semantic_episode_id"] == episode_id
    assert collapsed[0]["semantic_episode_events"] == [event_a, event_b]
    assert collapsed[0]["retrieval_reason"] == "semantic_episode"


def test_occurrence_and_episode_jobs_are_semantic_work():
    assert job_spec("materialize_event_occurrences").resource_class is ResourceClass.SEMANTIC
    assert job_spec("derive_semantic_episodes").resource_class is ResourceClass.SEMANTIC
