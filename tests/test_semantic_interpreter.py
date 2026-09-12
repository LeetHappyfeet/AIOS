from aios_app.epistemic.semantic_interpreter import interpret_frame


def test_want_is_desire_and_goal_semantics():
    result = interpret_frame(
        predicate="want",
        predicate_surface="want",
        subject="Shego",
        object_value="a physical body",
        frame_role="main",
        resolution_status="resolved",
    )
    assert result.semantic_type == "DESIRE"
    assert result.predicate_family == "GOAL"
    assert result.claim_kind == "GOAL"
    assert result.standalone_semantic is True


def test_copular_description_is_not_identity():
    result = interpret_frame(
        predicate="be_definition_of",
        predicate_surface="be",
        subject="Shego",
        object_value="digital",
        frame_role="main",
        resolution_status="resolved",
    )
    assert result.semantic_type == "DESCRIPTION"
    assert result.predicate_family == "DESCRIPTIVE"


def test_copular_role_can_be_identity_like():
    result = interpret_frame(
        predicate="be_definition_of",
        predicate_surface="be",
        subject="Shego",
        object_value="Ren's admin partner",
        frame_role="main",
        resolution_status="resolved",
    )
    assert result.semantic_type == "IDENTITY"


def test_dependent_clause_is_semantic_content_not_atomic_fact():
    result = interpret_frame(
        predicate="build",
        predicate_surface="build",
        subject="Ren",
        object_value="a body",
        frame_role="xcomp",
        resolution_status="resolved",
    )
    assert result.semantic_type == "ACTION"
    assert result.standalone_semantic is False


def test_internal_frame_reference_never_becomes_standalone():
    result = interpret_frame(
        predicate="want",
        predicate_surface="want",
        subject="Ren",
        object_value="frame:2",
        frame_role="main",
        resolution_status="resolved",
        object_frame_id="synthetic-child-id",
    )
    assert result.semantic_type == "DESIRE"
    assert result.standalone_semantic is False
    assert "nested_content" in result.cues
