"""Experiment 7 source-fidelity and HUD labeling regressions.

The observer is not the reported event's eyewitness, and a resolved speaker
name must never coexist with residual unbound first-person references.
"""
from aios_app.epistemic.semantic_integrity import validate_frame
from aios_app.hud.render_text import render_hud_text


def test_gym_anecdote_mixed_perspective_remains_incomplete():
    result = validate_frame(
        "I was doing a swim set and some guy told me my form was inspiring.",
        {"subject": "Alex", "predicate": "do",
         "object": "swim set and some guy told me my form was inspiring",
         "modality": "asserted"},
        speaker_id="Alex", source_subject="I", source_object="swim set",
    )
    assert result.status == "incomplete"
    assert "mixed_perspective_source_reference" in result.reasons


def test_first_person_role_does_not_bind_to_observer():
    result = validate_frame(
        "I checked my phone.",
        {"subject": "Renamon", "predicate": "check", "object": "phone",
         "modality": "asserted"},
        speaker_id="Alex", source_subject="I", source_object="phone",
    )
    assert result.status == "incomplete"
    assert "speaker_owned_subject_not_grounded" in result.reasons


def test_correctly_bound_source_speaker_is_not_rejected_by_ownership_guard():
    result = validate_frame(
        "I checked my phone.",
        {"subject": "Alex", "predicate": "check", "object": "phone",
         "modality": "asserted"},
        speaker_id="Alex", source_subject="I", source_object="phone",
    )
    assert "speaker_owned_subject_not_grounded" not in result.reasons
    assert "mixed_perspective_source_reference" not in result.reasons


def test_hud_marks_model_derived_report_as_not_direct_observation():
    text = render_hud_text({
        "identity": {"display_name": "Renamon"},
        "presence": {"instance_id": "fixture", "world_key": "world"},
        "beliefs": [{
            "text": "Alex described a swim set.",
            "epistemic_status": "observed",
            "effective_confidence": .86,
            "origin_kind": "model-inference", "epistemic_mode": "inference",
            "authority_state": "candidate",
        }],
        "memories": [{
            "text": "Alex reported what happened at the gym.",
            "epistemic_status": "observed",
            "origin_kind": "model-inference", "epistemic_mode": "inference",
        }],
    })
    assert text.count("underlying event not independently observed") == 2
    assert "[observed confidence=0.86]" not in text


def test_direct_observation_keeps_its_observed_label():
    text = render_hud_text({
        "identity": {"display_name": "Renamon"},
        "presence": {"instance_id": "fixture", "world_key": "world"},
        "beliefs": [{"text": "Renamon saw Alex.",
                     "epistemic_status": "observed",
                     "effective_confidence": .90,
                     "origin_kind": "direct-observation", "epistemic_mode": "observed"}],
    })
    assert "[observed confidence=0.90]" in text
