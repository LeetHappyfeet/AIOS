from aios_app.agent.cognitive_subjects import CognitiveSubject, SubjectKnowledgeDemandResolver


def _subject():
    from uuid import UUID
    return CognitiveSubject(
        subject_id=UUID("00000000-0000-0000-0000-000000000001"),
        canonical_key="entity:dr toros:about::",
        subject_type="entity",
        entity_keys=("dr toros","renamon"),
        predicate_key="medical contact",
        object_key=None,
        topic_key=None,
        question_type="relationship",
        question="What is Dr. Toros's relationship to Renamon?",
        display_label="Dr. Toros and Renamon",
        confidence=.6, uncertainty=.4, salience=1.0,
    )


def test_subject_demand_prefers_internal_evidence_before_corpus():
    demand=SubjectKnowledgeDemandResolver().resolve(_subject(),[
        {"subject_norm":"dr toros","predicate_norm":"asks about",
         "object_norm":"renamon","text":"Dr. Toros asked about Renamon."}
    ])
    assert demand["next_source"]=="memory"
    assert demand["matching"]


def test_subject_demand_builds_structured_corpus_query_when_unknown():
    demand=SubjectKnowledgeDemandResolver().resolve(_subject(),[])
    assert demand["next_source"]=="corpus"
    assert "dr toros" in demand["query"]
    assert "medical contact" in demand["query"]
