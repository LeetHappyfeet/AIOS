from aios_app.epistemic.belief_reconciliation import choose_belief_stance


def test_positive_stance_requires_support_and_margin():
    decision = choose_belief_stance(0.82, 0.20)
    assert decision.stance == "positive"
    assert round(decision.confidence, 2) == 0.62


def test_negative_stance_is_symmetric():
    decision = choose_belief_stance(0.10, 0.75)
    assert decision.stance == "negative"
    assert round(decision.confidence, 2) == 0.65


def test_conflicting_strong_evidence_remains_unresolved():
    decision = choose_belief_stance(0.90, 0.84)
    assert decision.stance == "unresolved"
    assert decision.confidence < 0.15


def test_weak_one_sided_evidence_does_not_become_belief():
    decision = choose_belief_stance(0.45, 0.0)
    assert decision.stance == "unresolved"


def test_support_is_clamped_to_probability_range():
    decision = choose_belief_stance(5.0, -2.0)
    assert decision.stance == "positive"
    assert decision.positive_support == 1.0
    assert decision.negative_support == 0.0
