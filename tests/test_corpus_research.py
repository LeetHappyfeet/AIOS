from aios_app.epistemic.cognitive_context import CognitiveAttentionInputs, automatic_corpus_research_allowed
from uuid import UUID

from aios_app.epistemic.research import (
    CorpusResearchHit,
    CorpusResearchResult,
    KnowledgeDemandResolver,
    CorpusLearningPolicy,
    SemanticKnowledgeCoverageService,
    SemanticCorpusReinforcementService,
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


def test_semantic_coverage_prefers_structured_roles_over_incidental_text():
    resolver = SemanticKnowledgeCoverageService()
    demand = resolver.resolve(
        "hematite oxidize",
        knowledge=[{
            "subject_norm": "hematite",
            "predicate_norm": "be",
            "object_norm": "iron oxide",
            "topic_key": "mineralogy",
            "text": "Hematite is an iron oxide mineral that can be discussed with oxidation.",
            "effective_confidence": 0.9,
        }],
        threshold=0.60,
    )
    assert "hematite" not in demand.missing_terms
    assert "oxidize" in demand.missing_terms


def test_semantic_reinforcement_terms_use_proposition_roles():
    terms = SemanticCorpusReinforcementService._knowledge_terms([{
        "subject_norm": "hematite",
        "predicate_norm": "cause",
        "object_norm": "red coloration",
        "topic_key": "geology",
        "text": "ignored presentation wording",
    }])
    assert {"hematite", "cause", "red", "coloration", "geology"} <= terms
    assert "ignored" not in terms


def _corpus_attention(*, speaker_id: str, speaker_role: str) -> CognitiveAttentionInputs:
    return CognitiveAttentionInputs(
        recent_newest=[{
            "speaker_id": speaker_id,
            "speaker_role": speaker_role,
            "message_text": "What was Renamon originally going to be named?",
        }],
        visible_source_node_ids=frozenset(),
        focus_text="What was Renamon originally going to be named?",
        plugin_focus_text="",
        retrieval_focus_text="What was Renamon originally going to be named?",
        goals=[],
    )


def test_automatic_corpus_research_allows_external_character_focus():
    attention = _corpus_attention(speaker_id="Alex_", speaker_role="character")
    assert automatic_corpus_research_allowed(attention, character_id="Renamon")


def test_automatic_corpus_research_rejects_character_own_output():
    attention = _corpus_attention(speaker_id="Renamon", speaker_role="character")
    assert not automatic_corpus_research_allowed(attention, character_id="Renamon")


def test_automatic_corpus_research_rejects_assistant_output():
    attention = _corpus_attention(speaker_id="Renamon", speaker_role="assistant")
    assert not automatic_corpus_research_allowed(attention, character_id="Renamon")


def test_domain_identifier_normalization_is_stable():
    from aios_app.corpus_routing import normalize_facet_value
    assert normalize_facet_value("  Star Wars   - All Media Types ") == "star wars - all media types"


def test_only_trusted_structured_fandom_identifiers_route_domains():
    from aios_app.corpus_routing import CorpusFacetRouter
    assert "fandom" in CorpusFacetRouter.ROUTABLE_FACET_TYPES
    assert "character" not in CorpusFacetRouter.ROUTABLE_FACET_TYPES
    assert "tag" not in CorpusFacetRouter.ROUTABLE_FACET_TYPES
