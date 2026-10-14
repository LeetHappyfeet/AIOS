from aios_app.hud.retrieval import MAX_FOCUS_TERMS, POLICIES, _RETRIEVAL_SQL, _focus_terms
from aios_app.epistemic.relevance import _continuity_distance
from aios_app.epistemic.retrieval import TOPOLOGY_SQL_TIMEOUT_SECONDS


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


def test_focus_terms_preserve_late_identifier_seed():
    terms = _focus_terms(
        "Mia sits up alright I'll go stand in the water with you do anything "
        "really it's your afternoon off. Mia takes off her sandals. Boy I wish "
        "Alex_ had been able to come, she loves the beach too."
    )
    assert "alex_" in terms
    assert len(terms) <= MAX_FOCUS_TERMS


def test_focus_terms_sample_across_long_turn():
    terms = _focus_terms(" ".join(f"term{index}" for index in range(40)))
    assert len(terms) == MAX_FOCUS_TERMS
    assert terms[-1] != "term11"
    assert any(int(term.removeprefix("term")) >= 30 for term in terms)


def test_topology_seed_budget_deduplicates_semantic_identity_before_limit():
    assert "diverse_seed_candidates AS" in _RETRIEVAL_SQL
    assert "COALESCE(proposition_id::text, node_type || ':' || node_key)" in _RETRIEVAL_SQL
    assert _RETRIEVAL_SQL.index("diverse_seed_candidates AS") < _RETRIEVAL_SQL.index("LIMIT 64")


def test_character_continuity_rank_is_not_treated_as_graph_depth():
    assert _continuity_distance(0) == 0
    assert _continuity_distance(1) == 1
    assert _continuity_distance(100001) == 2
    assert _continuity_distance(100031) == 32


def test_pre_generation_topology_sql_budget_is_five_seconds():
    assert TOPOLOGY_SQL_TIMEOUT_SECONDS == 5.0
