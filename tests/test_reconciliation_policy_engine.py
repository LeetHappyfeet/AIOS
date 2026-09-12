from aios_app.epistemic.reconciliation_policy import (
    EvidencePoint,
    aggregate_support,
    choose_stance,
    policy_for_family,
)


def test_identity_accumulates_independent_evidence():
    policy = policy_for_family("IDENTITY")
    positive, negative, count = aggregate_support(
        [
            EvidencePoint(1, 0.50, correlation_key="source:a"),
            EvidencePoint(1, 0.50, correlation_key="source:b"),
        ],
        policy,
    )
    assert policy.mode == "accumulate"
    assert round(positive, 2) == 0.75
    assert negative == 0.0
    assert count == 2
    assert choose_stance(positive, negative, policy)[0] == "positive"


def test_latest_state_does_not_accumulate_history():
    policy = policy_for_family("EMOTIONAL")
    positive, negative, count = aggregate_support(
        [
            EvidencePoint(1, 0.95, order=1.0, correlation_key="old"),
            EvidencePoint(-1, 0.70, order=2.0, correlation_key="new"),
        ],
        policy,
    )
    assert policy.mode == "latest"
    assert positive == 0.0
    assert negative == 0.70
    assert count == 1
    assert choose_stance(positive, negative, policy)[0] == "negative"


def test_rule_uses_strongest_evidence_not_repetition():
    policy = policy_for_family("RULE")
    positive, negative, count = aggregate_support(
        [
            EvidencePoint(1, 0.45, correlation_key="copy:1"),
            EvidencePoint(1, 0.45, correlation_key="copy:2"),
            EvidencePoint(1, 0.45, correlation_key="copy:3"),
        ],
        policy,
    )
    assert policy.mode == "max"
    assert positive == 0.45
    assert negative == 0.0
    assert count == 3
    assert choose_stance(positive, negative, policy)[0] == "unresolved"


def test_location_policy_is_exclusive_latest_state():
    policy = policy_for_family("SPATIAL")
    assert policy.mode == "latest"
    assert policy.exclusive_slot is True


def test_duplicate_correlation_coordinate_is_collapsed():
    policy = policy_for_family("IDENTITY")
    positive, _, count = aggregate_support(
        [
            EvidencePoint(1, 0.40, correlation_key="same-event"),
            EvidencePoint(1, 0.80, correlation_key="same-event"),
        ],
        policy,
    )
    assert positive == 0.80
    assert count == 1
