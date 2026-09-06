from uuid import uuid4

from aios_app.hud.context import HUDContext
from aios_app.hud.relevance import HUDRelevanceScorer
from aios_app.hud.render_text import render_hud_text


def _context():
    world_id = uuid4()
    parent_id = uuid4()
    entity_id = uuid4()
    return HUDContext(
        instance_id=uuid4(),
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
        "memories": [{"text": "Michael mentioned the basement."}],
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
    assert "KNOWLEDGE / BELIEFS:" in text
    assert "conflicts with: The door is open." in text
    assert "AVAILABLE ACTIONS: speak, inspect" in text
    assert "Stay inside this HUD's epistemic and branch boundaries." in text
