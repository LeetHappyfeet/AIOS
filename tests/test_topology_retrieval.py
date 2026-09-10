from aios_app.hud.retrieval import POLICIES, _focus_terms


def test_memory_retrieval_keeps_topic_history():
    policy = POLICIES["memory"]
    assert policy.retain_topic_history is True
    assert "MEMORY" in policy.claim_kinds
    assert policy.max_hops >= 2


def test_belief_retrieval_prefers_current_topic_head():
    policy = POLICIES["belief"]
    assert policy.retain_topic_history is False
    assert "BELIEF" in policy.claim_kinds


def test_rules_have_dedicated_shallow_policy():
    policy = POLICIES["rule"]
    assert policy.claim_kinds == ("RULE",)
    assert policy.max_hops == 1


def test_focus_terms_are_deduplicated_and_bounded():
    terms = _focus_terms(
        "John John station key",
        "Find Sarah at the station",
        " ".join(f"term{i}" for i in range(50)),
    )
    assert terms.count("john") == 1
    assert "station" in terms
    assert "sarah" in terms
    assert len(terms) <= 24
