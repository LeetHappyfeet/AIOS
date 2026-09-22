from uuid import UUID

from aios_app.epistemic.research import (
    CorpusResearchHit,
    CorpusResearchResult,
    KnowledgeDemandResolver,
    CorpusLearningPolicy,
    research_terms,
)


def test_research_terms_are_deterministic_and_drop_function_words():
    assert research_terms("How does hematite form in the red rocks?") == (
        "does",
        "hematite",
        "form",
        "red",
        "rocks",
    )


def test_knowledge_demand_detects_missing_concepts():
    resolver = KnowledgeDemandResolver(coverage_threshold=0.60)
    demand = resolver.resolve(
        "hematite iron oxide formation",
        known_texts=["iron oxide is familiar"],
    )
    assert demand.needed
    assert "hematite" in demand.missing_terms
    assert "formation" in demand.missing_terms


def test_knowledge_demand_stays_closed_when_coverage_is_sufficient():
    resolver = KnowledgeDemandResolver(coverage_threshold=0.60)
    demand = resolver.resolve(
        "hematite iron oxide",
        known_texts=["hematite is an iron oxide mineral"],
    )
    assert not demand.needed
    assert demand.coverage == 1.0


def test_reference_context_is_explicitly_non_durable():
    research_id = UUID("00000000-0000-0000-0000-000000000001")
    instance_id = UUID("00000000-0000-0000-0000-000000000002")
    hit = CorpusResearchHit(
        section_id=UUID("00000000-0000-0000-0000-000000000003"),
        document_id=UUID("00000000-0000-0000-0000-000000000004"),
        score=0.75,
        title="Mineralogy",
        heading="Hematite",
        excerpt="Hematite is an iron oxide mineral.",
        scopes=("science.geology",),
    )
    result = CorpusResearchResult(
        research_id=research_id,
        instance_id=instance_id,
        character_id="Renamon",
        query="hematite",
        terms=("hematite",),
        hits=(hit,),
        status="searched",
    )
    rendered = result.reference_context()
    assert rendered[0]["kind"] == "corpus_reference"
    assert rendered[0]["durable_knowledge"] is False
    assert rendered[0]["section_id"] == str(hit.section_id)


def test_hud_renderer_separates_corpus_reference_from_belief():
    from aios_app.hud.render_text import render_hud_text

    frame = {
        "identity": {"display_name": "Renamon"},
        "presence": {
            "world_key": "char:Renamon",
            "instance_id": "00000000-0000-0000-0000-000000000002",
            "state_version": 1,
        },
        "state": {},
        "scene": {},
        "beliefs": [{"text": "Iron can oxidize.", "epistemic_status": "known"}],
        "corpus_references": [{
            "title": "Mineralogy",
            "heading": "Hematite",
            "text": "Hematite is an iron oxide mineral.",
            "durable_knowledge": False,
        }],
        "actions": [],
    }
    rendered = render_hud_text(frame)
    assert "KNOWLEDGE / BELIEFS:" in rendered
    assert "Iron can oxidize." in rendered
    assert "CORPUS REFERENCES (LOOKED UP; NOT MEMORY OR BELIEF):" in rendered
    assert "[Mineralogy / Hematite] Hematite is an iron oxide mineral." in rendered


def test_corpus_learning_scope_value_prefers_most_specific_scope():
    mapping = {
        "science": 0.4,
        "science.geology": 0.9,
    }
    assert CorpusLearningPolicy._scope_value(
        mapping, ("science.geology.mineralogy",), 0.5
    ) == 0.9


def test_corpus_learning_scope_value_uses_default_without_profile_match():
    assert CorpusLearningPolicy._scope_value(
        {"history": 0.8}, ("science.geology",), 0.5
    ) == 0.5


def test_learning_decision_tracks_reinforcement_field():
    from aios_app.epistemic.research import CorpusLearningDecision

    decision = CorpusLearningDecision(
        section_id=UUID("00000000-0000-0000-0000-000000000005"),
        eligible=False,
        score=0.51,
        threshold=0.72,
        exposure_count=2,
        reinforcement=0.5,
        reason="reference_only",
    )
    assert decision.reinforcement == 0.5
    assert not decision.eligible
