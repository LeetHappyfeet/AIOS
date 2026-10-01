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
