from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_structured_retrieval_contract_separates_scope_and_query_kind():
    source = (ROOT / "epistemic" / "structured_retrieval.py").read_text()
    assert 'CHARACTER = "character"' in source
    assert 'WORLD = "world"' in source
    assert 'SEARCH = "search"' in source
    assert 'EVIDENCE = "evidence"' in source
    assert 'RELATION = "relation"' in source
    assert 'HISTORY = "history"' in source


def test_character_evidence_is_authority_filtered_in_sql():
    source = (ROOT / "epistemic" / "structured_retrieval.py").read_text()
    assert "JOIN aios.epistemic_authority_admission" in source
    assert "$3::text=ANY(eaa.authorized_uses)" in source
    assert '"retrieval_scope": "character"' in source
    assert "independent_evidence_count" in source


def test_relation_lookup_is_directed_bounded_and_scope_local():
    source = (ROOT / "epistemic" / "structured_retrieval.py").read_text()
    assert 'direction not in {"outgoing", "incoming", "either"}' in source
    assert "hops = max(1, min(int(max_hops), 2))" in source
    assert "e.scope_key=$1" in source
    assert "$6='outgoing' AND e.parent_node_id=w.node_id" in source
    assert "$6='incoming' AND e.child_node_id=w.node_id" in source


def test_temporal_lookup_uses_dag_not_rdf_or_vector():
    source = (ROOT / "epistemic" / "structured_retrieval.py").read_text()
    history = source[source.index("    async def history("):source.index("class EpistemicComparisonService")]
    assert "aios.dag_node" in history
    assert "SemanticQueryService" not in history
    assert "Fuseki" not in history


def test_epistemic_comparison_keeps_char_and_world_as_separate_inputs():
    source = (ROOT / "epistemic" / "structured_retrieval.py").read_text()
    compare = source[source.index("class EpistemicComparisonService"):]
    assert "aios.character_proposition_knowledge" in compare
    assert "aios.world_proposition_assertion" in compare
    assert "'unverified'" in compare
    assert "'corroborated'" in compare
    assert "'contradicted'" in compare
    assert "char_polarities && w.world_polarities" in compare


def test_agent_lookup_surface_exposes_structured_operations_without_sparql():
    source = (ROOT / "agent" / "capabilities.py").read_text()
    assert '"operation":{"type":"string","enum":["search","evidence","relation","history"]}' in source
    assert '"operation":{"type":"string","enum":["search","evidence","relation"]}' in source
    assert 'name="epistemic.compare"' in source
    assert "SPARQL" not in source
    assert "knowledge.lookup" in source
    assert "world.lookup" in source
