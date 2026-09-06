from aios_app.semantic_index.classifier import (
    BoundaryFeatures,
    _choose_label,
    _score_boundary,
    _weighted_overlap,
)


def test_missing_distribution_overlap_is_neutral():
    assert _weighted_overlap({}, {}) == 0.5


def test_state_transition_beats_generic_temporal_transition_for_state_like_series():
    features = BoundaryFeatures(
        mean_similarity=0.78,
        max_similarity=0.86,
        edge_count=4,
        topic_overlap=0.90,
        subject_overlap=0.95,
        predicate_overlap=0.90,
        source_overlap=0.80,
        world_overlap=1.0,
        timeline_overlap=1.0,
        character_instance_overlap=0.90,
        origin_character_overlap=1.0,
        temporal_overlap=0.0,
        temporal_separation=0.75,
        conflict_density=0.25,
        exclusive_conflict_density=0.25,
        opposite_polarity_density=0.0,
        state_like_ratio=0.95,
    )
    scores = _score_boundary(features)
    assert scores["STATE_TRANSITION"] > scores["TEMPORAL_TRANSITION"]


def test_same_region_wins_when_clusters_are_semantically_and_contextually_aligned():
    features = BoundaryFeatures(
        mean_similarity=0.92,
        max_similarity=0.97,
        edge_count=8,
        topic_overlap=0.90,
        subject_overlap=0.90,
        predicate_overlap=0.85,
        source_overlap=0.85,
        world_overlap=1.0,
        timeline_overlap=1.0,
        character_instance_overlap=1.0,
        origin_character_overlap=1.0,
        temporal_overlap=0.75,
        temporal_separation=0.0,
        conflict_density=0.0,
        exclusive_conflict_density=0.0,
        opposite_polarity_density=0.0,
        state_like_ratio=0.20,
    )
    scores = _score_boundary(features)
    label, confidence = _choose_label(
        scores,
        min_confidence=0.48,
        min_margin=0.04,
    )
    assert label == "SAME_REGION"
    assert confidence >= 0.48


def test_ambiguous_boundary_remains_unresolved():
    scores = {
        "TOPIC_SPLIT": 0.56,
        "NARRATIVE_SPLIT": 0.54,
        "SAME_REGION": 0.30,
    }
    label, confidence = _choose_label(
        scores,
        min_confidence=0.48,
        min_margin=0.04,
    )
    assert label == "UNRESOLVED"
    assert confidence == 0.56
