from uuid import uuid4

from aios_app.hud.frame import (
    _apply_cognitive_firewall,
    _hud_candidate_rejection_reason,
)


def _item(**overrides):
    item = {
        "proposition_id": uuid4(),
        "text": "alex | know | mia",
        "subject_norm": "alex",
        "predicate_norm": "know",
        "object_norm": "mia",
        "claim_kind": "BELIEF",
        "effective_confidence": 0.65,
        "source_node_id": uuid4(),
    }
    item.update(overrides)
    return item


def test_firewall_suppresses_current_source_decomposition():
    node_id = uuid4()
    item = _item(source_node_id=node_id)

    assert _hud_candidate_rejection_reason(
        item,
        visible_source_node_ids=frozenset({str(node_id)}),
    ) == "current_source_duplicate"


def test_firewall_suppresses_structurally_incomplete_belief():
    assert _hud_candidate_rejection_reason(
        _item(object_norm=None, text="mia | know | _"),
    ) == "missing_object"


def test_firewall_suppresses_unresolved_nested_frame():
    assert _hud_candidate_rejection_reason(
        _item(text="alex | greet | frame:1"),
    ) == "unresolved_frame"


def test_firewall_suppresses_nonpositive_confidence():
    assert _hud_candidate_rejection_reason(
        _item(effective_confidence=0.0),
    ) == "nonpositive_confidence"


def test_firewall_admits_well_formed_older_knowledge():
    item = _item()

    assert _hud_candidate_rejection_reason(item) is None


def test_firewall_reports_suppression_reasons_without_mutating_items():
    current_node = uuid4()
    good = _item(text="alex | remember | orientation", predicate_norm="remember", object_norm="orientation")
    duplicate = _item(source_node_id=current_node)
    malformed = _item(subject_norm="*", text="* | be | alex")

    admitted, suppressed = _apply_cognitive_firewall(
        [good, duplicate, malformed],
        visible_source_node_ids=frozenset({str(current_node)}),
    )

    assert admitted == [good]
    assert suppressed == {
        "current_source_duplicate": 1,
        "missing_subject": 1,
    }
    assert duplicate["source_node_id"] == current_node
    assert malformed["subject_norm"] == "*"
