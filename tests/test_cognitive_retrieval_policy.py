from aios_app.epistemic.retrieval_policy import (
    CognitiveRetrievalPolicy,
    DEFAULT_COGNITIVE_RETRIEVAL_POLICY,
)


def test_default_cognitive_retrieval_policy_preserves_prior_default_depths():
    policy = DEFAULT_COGNITIVE_RETRIEVAL_POLICY
    assert policy.effective_memory_hops == 2
    assert policy.belief_hops == 2
    assert policy.event_hops == 2
    assert policy.goal_hops == 1
    assert policy.rule_hops == 1
    assert policy.semantic_retrieval_limit == 25
    assert policy.memory_limit == 25
    assert policy.recent_context_limit == 12


def test_deep_memory_expands_memory_without_hud_profile():
    policy = CognitiveRetrievalPolicy(
        memory_hops=2,
        semantic_retrieval_limit=25,
        deep_memory_limit=20,
    )
    assert policy.effective_memory_hops == 3
    assert policy.memory_limit == 45
