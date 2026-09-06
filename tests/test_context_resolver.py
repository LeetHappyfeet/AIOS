from aios_app.models import ExternalObservationIn
from aios_app.epistemic.normalizer import normalize_components
from aios_app.epistemic.context_resolver import (
    classify_claim_kind,
    classify_entity_kind,
    classify_predicate_family,
    is_semantic_pivot,
    resolve_ingest_viewpoint,
)


def test_memory_and_belief_are_distinct_epistemic_classes():
    assert classify_predicate_family("remember", "I remember the docks.") == "MEMORY"
    assert classify_claim_kind("MEMORY", "remember", "I remember the docks.") == "MEMORY"

    assert classify_predicate_family("believe", "I believe Bob lied.") == "EPISTEMIC"
    assert classify_claim_kind("EPISTEMIC", "believe", "I believe Bob lied.") == "BELIEF"


def test_spatial_object_becomes_location_and_pivot():
    family = classify_predicate_family("located_at", "Bob is located at the tavern.")
    kind = classify_entity_kind("Tavern", role="object", predicate_family=family)
    assert family == "SPATIAL"
    assert kind == "LOCATION"
    assert is_semantic_pivot(kind, role="object", predicate_family=family) is True


def test_known_character_is_person_pivot():
    kind = classify_entity_kind(
        "alice",
        role="subject",
        predicate_family="EMOTIONAL",
        is_known_character=True,
    )
    assert kind == "PERSON"
    assert is_semantic_pivot(kind, role="subject", predicate_family="EMOTIONAL") is True


def test_unknown_entity_is_not_guessed_into_shared_world_identity():
    kind = classify_entity_kind(
        "mysterious thing",
        role="object",
        predicate_family="UNKNOWN",
        is_known_character=False,
    )
    assert kind == "UNKNOWN"
    assert is_semantic_pivot(kind, role="object", predicate_family="UNKNOWN") is False


def test_rule_and_goal_detection_from_raw_text():
    assert classify_predicate_family(None, "Guests must leave before dawn.") == "RULE"
    assert classify_predicate_family(None, "Alice plans to find Bob.") == "GOAL"


def test_same_statement_normalizes_to_one_source_neutral_proposition():
    alice = normalize_components(
        subject="king",
        predicate="be",
        object_value="dead",
        raw_text="The king is dead.",
    )
    bob = normalize_components(
        subject="king",
        predicate="be",
        object_value="dead",
        raw_text="The king is dead.",
    )

    # Character ownership is deliberately absent from proposition identity.
    # Separate observations/knowledge rows carry Alice/Bob visibility instead.
    assert alice["proposition_hash"] == bob["proposition_hash"]
    assert alice["topic_key"] == bob["topic_key"]


def test_external_source_speaker_does_not_become_character_viewpoint():
    viewpoint = resolve_ingest_viewpoint(
        explicit_viewpoint_id=None,
        speaker_id="fox_news",
        speaker_type="source",
        origin_character_id=None,
    )
    assert viewpoint is None


def test_character_speaker_retains_first_person_viewpoint():
    viewpoint = resolve_ingest_viewpoint(
        explicit_viewpoint_id=None,
        speaker_id="alice",
        speaker_type="character",
        origin_character_id="alice",
    )
    assert viewpoint == "alice"


def test_explicit_external_viewpoint_is_preserved_without_character_ownership():
    viewpoint = resolve_ingest_viewpoint(
        explicit_viewpoint_id="reporter:jane_doe",
        speaker_id="news_org",
        speaker_type="source",
        origin_character_id=None,
    )
    assert viewpoint == "reporter:jane_doe"


def test_external_observation_contract_defaults_to_source_liminal_event():
    req = ExternalObservationIn(
        source_id="cnn",
        source_kind="news_organization",
        speaker_id="cnn",
        text="Example observation.",
    )
    assert req.kind == "observation"
    assert req.speaker_type == "source"
    assert req.target_character_id is None
    assert req.target_world_id is None
