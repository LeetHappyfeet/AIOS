from uuid import uuid4

from aios_app.hud.context import HUDContext
from aios_app.hud.relevance import HUDRelevanceScorer
from aios_app.hud.render_text import render_hud_text
from aios_app.epistemic.retrieval import TopologyRetriever


def _context():
    world_id = uuid4()
    parent_id = uuid4()
    entity_id = uuid4()
    instance_id = uuid4()
    return HUDContext(
        instance_id=instance_id,
        character_id="natalie",
        entity_id=entity_id,
        world_id=world_id,
        world_key="char:natalie:session:test",
        timeline_id=uuid4(),
        head_node_id=uuid4(),
        source_timeline_id=uuid4(),
        source_head_node_id=uuid4(),
        state_version=7,
        lifecycle_state="ready",
        location_entity_id=None,
        lineage_world_ids=(world_id, parent_id),
        lineage_instance_ids=(instance_id,),
        scene_entity_ids=frozenset({entity_id}),
    )


def test_relevance_rejects_sibling_world():
    context = _context()
    scorer = HUDRelevanceScorer(context, focus_text="red backpack basement key")
    sibling_world = uuid4()

    score = scorer.score(
        {},
        candidate_text="red backpack contains basement key",
        candidate_world_id=sibling_world,
    )

    assert score.branch_penalty == 100.0
    assert score.total < 0.0


def test_relevance_allows_ancestor_world():
    context = _context()
    scorer = HUDRelevanceScorer(context)
    score = scorer.score({}, candidate_world_id=context.lineage_world_ids[1])
    assert score.branch_penalty == 0.0


def test_text_renderer_uses_canonical_hud_sections():
    frame = {
        "identity": {"character_id": "natalie", "display_name": "Natalie"},
        "presence": {
            "world_key": "test-world",
            "instance_id": uuid4(),
            "state_version": 2,
        },
        "scene": {
            "actors": [{"display_name": "Michael"}],
            "objects": [{"display_name": "red backpack"}],
        },
        "state": {"health": 90, "emotional": {"mood": "wary"}},
        "relationships": [
            {
                "display_name": "Michael",
                "relationship_type": "friend",
                "trust": 0.4,
            }
        ],
        "inventory": [{"display_name": "key", "quantity": 1, "equipped": False}],
        "memories": [
            {
                "text": "Michael mentioned the basement.",
                "anchor": {
                    "relationship": "remembers",
                    "target_label": "basement",
                    "world_visible": True,
                },
                "world_context": [
                    {"node_type": "LOCATION", "label": "old house", "edge_type": "about_topic"}
                ],
            }
        ],
        "beliefs": [
            {
                "text": "The door is locked.",
                "epistemic_status": "believed",
                "conflicts": [{"text": "The door is open."}],
            }
        ],
        "goals": [{"text": "Find the basement."}],
        "rules": [{"text": "Do not reveal hidden branch knowledge."}],
        "recent_events": [
            {
                "speaker_role": "user",
                "speaker_id": "Michael",
                "message_text": "Look at the door.",
            }
        ],
        "actions": ["speak", "inspect"],
    }

    text = render_hud_text(frame)

    assert "ACTIVE MEMORY:" in text
    assert "[remembers; about basement; context: old house]" in text
    assert "KNOWLEDGE / BELIEFS:" in text
    assert "conflicts with: The door is open." in text
    assert "AVAILABLE ACTIONS: speak, inspect" in text
    assert "Stay inside this HUD's epistemic and branch boundaries." in text


def test_instance_visibility_rejects_sibling_branch():
    context = _context()
    assert context.instance_visible(context.instance_id) is True
    assert context.instance_visible(uuid4()) is False


def test_text_renderer_withholds_invisible_world_neighbors():
    frame = {
        "identity": {"character_id": "natalie"},
        "presence": {"world_key": "branch-a", "instance_id": uuid4(), "state_version": 1},
        "state": {},
        "scene": {},
        "memories": [
            {
                "text": "A different branch contained a red door.",
                "anchor": {
                    "relationship": "perceived",
                    "target_label": "red door",
                    "world_visible": False,
                },
                "world_context": [
                    {"node_type": "LOCATION", "label": "secret sibling location"}
                ],
            }
        ],
    }

    text = render_hud_text(frame)

    assert "anchored context outside visible world; neighbors withheld" in text
    assert "secret sibling location" not in text


def test_canonical_event_collapse_returns_one_recall_candidate():
    event_id = uuid4()
    prop_a = uuid4()
    prop_b = uuid4()
    dag_node_id = uuid4()
    items = [
        {
            "proposition_id": prop_a,
            "claim_kind": "EVENT",
            "text": "shego_001 | set | the alarm",
            "relevance": {"total": 2.0},
        },
        {
            "proposition_id": prop_b,
            "claim_kind": "EVENT",
            "text": "shego_001 | set | three alarms",
            "relevance": {"total": 2.0},
        },
    ]
    event = {
        "semantic_event_id": event_id,
        "event_confidence": 0.66,
        "dag_node_id": dag_node_id,
        "member_proposition_ids": [prop_a, prop_b],
    }
    collapsed = TopologyRetriever._collapse_canonical_events(
        items, {prop_a: event, prop_b: event}
    )

    assert len(collapsed) == 1
    assert collapsed[0]["semantic_event_id"] == event_id
    assert collapsed[0]["text"] == "shego_001 | set | three alarms"
    assert collapsed[0]["semantic_event_members"] == [prop_a, prop_b]
    assert collapsed[0]["retrieval_reason"] == "canonical_semantic_event"


def test_canonical_event_collapse_leaves_unconsolidated_event_available():
    prop_id = uuid4()
    item = {
        "proposition_id": prop_id,
        "claim_kind": "EVENT",
        "text": "alex | open | the door",
        "relevance": {"total": 1.5},
    }

    assert TopologyRetriever._collapse_canonical_events([item], {}) == [item]
