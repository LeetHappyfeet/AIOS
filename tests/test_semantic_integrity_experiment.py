"""Source-fidelity regressions captured from Renamon participation experiment.

These are expected semantic constraints, not exact canonical-string golden files.
A frame that satisfies the SQL schema but violates a constraint is not correct.
"""
from aios_app.epistemic.semantic_integrity import validate_frame

CASES = [
    ("It's nice out.", {"subject": "dustin", "predicate": "be", "object": "nice"}, "invalid"),
    ("Instead she glances toward the open barn door.", {"subject": "he", "predicate": "glance", "object": "the open barn door"}, "invalid"),
    ("She watches him for a beat.", {"subject": "she", "predicate": "watch", "object": "she"}, "invalid"),
    ("Which is why I walked down here instead of waiting for you to come find me.", {"subject": "which", "predicate": "be", "object": None}, "invalid"),
    ("Her tail sweeps once, slow, curling near her hip.", {"subject": "her tail", "predicate": "sweep", "object": None}, "valid"),
    ("The host is listening.", {"subject": "the host", "predicate": "listen", "object": None}, "valid"),
    ("I don't wander.", {"subject": "renamon", "predicate": "wander", "object": None, "polarity": -1}, "valid"),
]

def test_experiment_source_integrity_examples():
    for source, frame, expected in CASES:
        result = validate_frame(source, frame, speaker_id="Renamon")
        assert result.status == expected, (source, result)

def test_missing_object_not_always_invalid():
    for predicate, subject in (("sleep", "renamon"), ("listen", "the host"),
                               ("tilt", "her head"), ("sweep", "her tail")):
        result = validate_frame(f"{subject} {predicate}s.", {"subject": subject, "predicate": predicate, "object": None})
        assert result.status != "invalid"

def test_duplicate_revision_is_stable():
    source = "It's nice out."
    frame = {"subject": "dustin", "predicate": "be", "object": "nice"}
    assert validate_frame(source, frame) == validate_frame(source, frame)


def test_george_renamon_source_fidelity_regressions():
    cases = [
        ("Either will do.", {"subject": "either", "predicate": "do", "object": None},
         "incomplete", "unresolved_discourse_subject"),
        ("I'm going to need the couch.", {"subject": "renamon", "predicate": "go", "object": None},
         "invalid", "future_auxiliary_misread_as_action"),
        ("But you need to leave before my mother sees you.",
         {"subject": "renamon", "predicate": "need", "object": None},
         "incomplete", "modal_action_argument_lost"),
        ("He gestured at the tail and the ears, and immediately regretted the gesture.",
         {"subject": "the ears", "predicate": "regret", "object": "the gesture"},
         "invalid", "source_actor_mismatch"),
    ]
    for source, frame, status, reason in cases:
        receipt = validate_frame(source, frame, speaker_id="George Constanza")
        assert receipt.status == status, (source, receipt)
        assert reason in receipt.reasons


def test_source_context_changes_receipt_revision():
    from aios_app.epistemic.semantic_integrity import revision_key
    frame = [{"subject": "renamon", "predicate": "need", "object": None}]
    assert revision_key("You need to leave.", frame, context_digest="first") != (
        revision_key("You need to leave.", frame, context_digest="second")
    )
