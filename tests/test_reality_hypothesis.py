from aios_app.epistemic.reality import plan_reality_memberships


def _by_role(plans):
    return {plan.context_role: plan for plan in plans}


def test_external_source_stays_hypothetical_for_target_world():
    plans = _by_role(
        plan_reality_memberships(
            {
                "epistemic_scope": "source",
                "source_id": "phantom_wiki",
                "source_kind": "community_wiki",
                "world_id": "00000000-0000-0000-0000-000000000001",
            }
        )
    )

    assert plans["source"].membership_status == "member"
    assert plans["source"].affinity >= 0.95
    assert plans["world"].membership_status == "candidate"
    assert plans["world"].affinity < 0.95


def test_external_narrative_does_not_become_concrete_world_member():
    plans = _by_role(
        plan_reality_memberships(
            {
                "epistemic_scope": "narrative",
                "source_id": "example_news",
                "source_kind": "news_organization",
                "world_id": "00000000-0000-0000-0000-000000000001",
            }
        )
    )

    assert plans["source"].membership_status == "member"
    assert plans["world"].membership_status == "candidate"
    assert plans["world"].reason == "external_narrative_world_hypothesis"


def test_runtime_narrative_remains_fast_authoritative_world_path():
    plans = _by_role(
        plan_reality_memberships(
            {
                "epistemic_scope": "narrative",
                "source_id": "sillytavern",
                "source_kind": "sillytavern",
                "world_id": "00000000-0000-0000-0000-000000000001",
            }
        )
    )

    assert plans["world"].membership_status == "member"
    assert plans["world"].affinity >= 0.95
    assert plans["world"].confidence >= 0.95


def test_explicit_world_observation_is_authoritative():
    plans = _by_role(
        plan_reality_memberships(
            {
                "epistemic_scope": "observation",
                "world_id": "00000000-0000-0000-0000-000000000001",
            }
        )
    )

    assert plans["world"].membership_status == "member"
    assert plans["world"].affinity == 1.0
    assert plans["world"].confidence == 1.0


def test_character_belief_is_compatible_not_objective_world_state():
    plans = _by_role(
        plan_reality_memberships(
            {
                "epistemic_scope": "character",
                "origin_character_id": "Shego_001",
                "world_id": "00000000-0000-0000-0000-000000000001",
            }
        )
    )

    assert plans["world"].membership_status == "compatible"
    assert plans["world"].affinity < 0.95


def test_target_world_hint_is_used_without_promoting_source_to_truth():
    plans = _by_role(
        plan_reality_memberships(
            {
                "epistemic_scope": "source",
                "source_id": "phantom_wiki",
                "source_kind": "community_wiki",
                "target_world_id": "00000000-0000-0000-0000-000000000002",
                "world_id": None,
            }
        )
    )

    assert plans["source"].membership_status == "member"
    assert plans["world"].membership_status == "candidate"
