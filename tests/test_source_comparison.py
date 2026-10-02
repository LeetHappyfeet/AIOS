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
