from aios_app.epistemic.knowledge import _context_acquisition_eligible_values


def test_read_allows_assertive_concept_claim_for_target_instance():
    assert _context_acquisition_eligible_values(
        instance_id="00000000-0000-0000-0000-000000000001",
        epistemic_scope="character",
        character_instance_id="00000000-0000-0000-0000-000000000001",
        claim_kind="CONCEPT",
        raw_text="Basalt is an igneous rock.",
        discourse_mode="narrated_observation",
        acquisition_mode="read",
    )


def test_read_does_not_cross_character_instance_boundary():
    assert not _context_acquisition_eligible_values(
        instance_id="00000000-0000-0000-0000-000000000001",
        epistemic_scope="character",
        character_instance_id="00000000-0000-0000-0000-000000000002",
        claim_kind="CONCEPT",
        raw_text="Basalt is an igneous rock.",
        discourse_mode="narrated_observation",
        acquisition_mode="read",
    )


def test_read_does_not_learn_questions_as_facts():
    assert not _context_acquisition_eligible_values(
        instance_id="00000000-0000-0000-0000-000000000001",
        epistemic_scope="character",
        character_instance_id="00000000-0000-0000-0000-000000000001",
        claim_kind="CONCEPT",
        raw_text="Is basalt an igneous rock?",
        discourse_mode="question",
        acquisition_mode="read",
    )


def test_normal_context_still_rejects_concept_claim():
    assert not _context_acquisition_eligible_values(
        instance_id="00000000-0000-0000-0000-000000000001",
        epistemic_scope="character",
        character_instance_id="00000000-0000-0000-0000-000000000001",
        claim_kind="CONCEPT",
        raw_text="Basalt is an igneous rock.",
        discourse_mode="narrated_observation",
        acquisition_mode="context_observation",
    )
