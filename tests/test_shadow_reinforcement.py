from uuid import uuid4

import pytest

from aios_app.agent.reinforcement import appraise, validate_expectation
from aios_app.epistemic.goal_scene_reconciliation import SceneGoalReconciler


def outcome(**changes):
    return {'attempt_id': uuid4(), 'verification': 'verified', 'outcome_type': 'success',
            'attribution': 'self', 'cause_class': 'achieved', **changes}


def test_prediction_error_requires_preexisting_expectation():
    expected = appraise(outcome(), {'success_probability': .9, 'importance': 1}, 'memory-first', receipt_terminal=True)
    surprise = appraise(outcome(), {'success_probability': .1, 'importance': 1}, 'memory-first', receipt_terminal=True)
    assert expected.magnitude == .025
    assert surprise.magnitude == .225
    ordinary = appraise(outcome(), {}, 'memory-first', receipt_terminal=True)
    assert ordinary.signal_type == 'outcome_feedback'
    assert ordinary.magnitude == .125


@pytest.mark.parametrize('changes', [
    {'verification': 'reviewed'}, {'verification': 'projected'},
    {'attribution': 'external'}, {'attribution': 'unknown'}, {'attribution': 'mixed'},
    {'cause_class': 'external_obstruction'}, {'outcome_type': 'abandoned'},
    {'outcome_type': 'unresolved'},
])
def test_unsupported_credit_is_observational(changes):
    signal = appraise(outcome(**changes), {}, 'memory-first', receipt_terminal=True)
    assert signal.target_type == 'observation'
    assert signal.magnitude == 0


def test_goal_success_without_executed_strategy_never_earns_strategy_credit():
    assert appraise(outcome(), {}, None, receipt_terminal=True).magnitude == 0
    assert appraise(outcome(), {}, 'memory-first', receipt_terminal=False).magnitude == 0


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 2, True, '0.5'])
def test_invalid_expectations_are_rejected(value):
    with pytest.raises(ValueError):
        validate_expectation({'success_probability': value})


def test_scene_contract_does_not_treat_missing_slot_as_negative_evidence():
    assert SceneGoalReconciler._matches({}, {'slot': 'location', 'operator': 'not_equals', 'value': 'shop'}) == (False, None)
    assert not SceneGoalReconciler._matches({'location': 123}, {'slot': 'location', 'operator': 'contains', 'value': '2'})[0]
    assert not SceneGoalReconciler._matches({'location': False}, {'slot': 'location', 'value': 0})[0]
