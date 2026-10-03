"""Participation must not reimplement grammatical argument validation."""
from aios_app.agent.participation import propose_v2

EMPTY = {"names":["Renamon"],"goals":[],"facets":[],"relationships":[],"truncated":{}}

def test_intransitive_listening_is_not_semantically_rejected():
    claim = dict(subject_norm="the host",predicate_norm="listen",object_norm=None,
                 speaker_id="Dustin", semantic_integrity_status="valid")
    result=propose_v2(claim,EMPTY,recurrence=1)
    assert result["signals"]["representation_quality"]["usable"] is True

def test_invalid_integrity_cannot_enter_foreground_even_if_relevant():
    context={**EMPTY,"goals":[{"goal_id":"g","goal_text":"Renamon watch barn door"}]}
    claim=dict(subject_norm="Renamon",predicate_norm="watch",object_norm="barn door",
               speaker_id="Renamon",canonical_text="Renamon watch barn door",
               semantic_integrity_status="invalid")
    result=propose_v2(claim,context,recurrence=4)
    assert result["population"] != "foreground"
    assert "source_integrity_invalid" in result["signals"]["representation_quality"]["reasons"]
