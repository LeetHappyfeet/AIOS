from uuid import uuid4

from aios_app.epistemic.topology import choose_observation_scope


def test_character_owned_observation_builds_character_scope():
    instance_id = uuid4()
    world_id = uuid4()
    decision = choose_observation_scope({
        "claim_kind": "MEMORY",
        "predicate_family": "MEMORY",
        "epistemic_scope": "character",
        "origin_character_id": "natalie",
        "character_instance_id": instance_id,
        "world_id": world_id,
        "source_id": None,
        "subject_is_pivot": True,
        "object_is_pivot": False,
    })
    assert decision.scope_kind == "character"
    assert decision.character_id == "natalie"
    assert decision.character_instance_id == instance_id
    assert decision.world_id == world_id
    assert decision.branch_kind == "epistemic_transition"
    assert decision.significance == 0.95


def test_external_source_never_becomes_character_or_target_world_scope():
    target_world_id = uuid4()
    decision = choose_observation_scope({
        "claim_kind": "EVENT",
        "predicate_family": "COMMUNICATION",
        "epistemic_scope": "source",
        "origin_character_id": None,
        "character_instance_id": None,
        "world_id": uuid4(),
        "source_id": "fox_news",
        "target_character_id": "natalie",
        "target_world_id": target_world_id,
        "subject_is_pivot": True,
        "object_is_pivot": False,
    })
    assert decision.scope_kind == "source"
    assert decision.scope_key == "source:fox_news"
    assert decision.character_id is None
    assert decision.world_id is None
    assert decision.branch_kind == "event"


def test_temporal_predicate_creates_temporal_transition():
    decision = choose_observation_scope({
        "claim_kind": "STATE",
        "predicate_family": "TEMPORAL",
        "epistemic_scope": "source",
        "source_id": "sensor:a",
        "subject_is_pivot": False,
        "object_is_pivot": False,
    })
    assert decision.scope_kind == "source"
    assert decision.branch_kind == "temporal_transition"


def test_semantic_entity_pivot_is_not_promoted_to_world_fork():
    decision = choose_observation_scope({
        "claim_kind": "STATE",
        "predicate_family": "SPATIAL",
        "epistemic_scope": "source",
        "source_id": "wiki:a",
        "subject_is_pivot": True,
        "object_is_pivot": True,
    })
    assert decision.branch_kind == "semantic_pivot"
    assert decision.scope_kind == "source"
