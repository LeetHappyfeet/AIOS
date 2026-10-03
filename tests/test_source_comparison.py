"""Read-only semantic audit must stay source-bounded and non-authoritative."""
from aios_app.epistemic.source_comparison import (
    bounded_source_context, build_comparison_prompt,
)


def test_source_window_includes_sentence_not_future_node():
    section = ("Earlier context. " * 100) + "But you need to leave before my mother sees you." + (" Later context." * 100)
    excerpt = bounded_source_context(section, "But you need to leave before my mother sees you.")
    assert "But you need to leave before my mother sees you." in excerpt
    assert len(excerpt) < len(section)


def test_audit_prompt_distinguishes_request_from_adopted_goal():
    prompt = build_comparison_prompt(
        source="But you need to leave before my mother sees you.",
        section="George turns to Renamon. But you need to leave before my mother sees you.",
        speaker="George Constanza", recipient="Renamon",
        frames=[{"subject": "renamon", "predicate": "need", "object": None}],
    )
    assert "not approval" in prompt.lower()
    assert "George Constanza" in prompt
    assert "goal adopted" in prompt
    assert "But you need to leave" in prompt


def test_context_excludes_text_after_target_sentence():
    section = "Earlier context. My mother will say 'Georgie.' LATER EVENT NOT YET KNOWN."
    excerpt = bounded_source_context(section, "My mother will say 'Georgie.'")
    assert "Earlier context." in excerpt
    assert "My mother will say 'Georgie.'" in excerpt
    assert "LATER EVENT NOT YET KNOWN" not in excerpt


def test_missing_source_anchor_does_not_invent_context():
    assert bounded_source_context("Subsequent events only.", "Original source.") == "Original source."


def test_review_writer_is_revision_guarded_and_non_authoritative():
    import inspect
    from aios_app.epistemic.source_comparison import audit_claim_with_local_inference
    code = inspect.getsource(audit_claim_with_local_inference)
    assert "si.revision_key=$2" in code
    assert "claim_source_comparison_audit" in code
    assert "character_agent_goal" not in code
    assert "proposition_evidence" not in code
