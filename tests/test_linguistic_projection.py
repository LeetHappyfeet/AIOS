from aios_app.epistemic import semantic_frames
from aios_app.epistemic import semantic_frames_legacy as legacy
from aios_app.epistemic.linguistic_projection import (
    deserialize_doc,
    parse_text,
    sentence_docs,
    serialize_doc,
)


def _signature(frames):
    return [
        (
            frame.index,
            frame.parent_index,
            frame.object_frame_index,
            frame.subject,
            frame.predicate_surface,
            frame.predicate_canonical,
            frame.object_text,
            frame.polarity,
            frame.modality,
            frame.tense,
            frame.aspect,
            frame.frame_role,
            frame.discourse_mode,
            frame.canonical_text,
            frame.meta.get("named_entities"),
            frame.meta.get("inside_direct_quote"),
            frame.meta.get("perspective_kind"),
            frame.meta.get("perspective_holder_text"),
        )
        for frame in frames
    ]


def test_projection_json_round_trip_preserves_sentence_annotations():
    text = 'Alice says, "I saw Bob." She does not trust him.'
    original = parse_text(text)
    restored = deserialize_doc(serialize_doc(original))

    original_sentences = sentence_docs(original)
    restored_sentences = sentence_docs(restored)

    assert [item.text for item in restored_sentences] == [item.text for item in original_sentences]
    assert [
        [(tok.text, tok.lemma_, tok.pos_, tok.dep_, tok.head.i, str(tok.morph)) for tok in item.doc]
        for item in restored_sentences
    ] == [
        [(tok.text, tok.lemma_, tok.pos_, tok.dep_, tok.head.i, str(tok.morph)) for tok in item.doc]
        for item in original_sentences
    ]
    assert [
        [(ent.text, ent.label_) for ent in item.doc.ents]
        for item in restored_sentences
    ] == [
        [(ent.text, ent.label_) for ent in item.doc.ents]
        for item in original_sentences
    ]


def test_projected_sentence_matches_legacy_semantic_frame_output():
    sentences = [
        'Alice says, "I saw Bob."',
        "Shego thought Kim had left, but she was wrong.",
        "The man who was shot dead by federal agents was identified.",
        "Pretti is believed to be an American citizen.",
        "Mark had no idea why Nina kept ringing but it probably wasn't good.",
    ]
    section_doc = parse_text(" ".join(sentences))
    projected = sentence_docs(section_doc)

    assert len(projected) == len(sentences)

    semantic_frames._install_projection_aware_nlp()

    for expected_text, projected_sentence in zip(sentences, projected):
        assert projected_sentence.text == expected_text

        # Baseline: force the underlying spaCy Language object to parse the
        # sentence independently, matching the pre-projection behavior.
        wrapper = legacy._NLP
        baseline = legacy.decompose_sentence(expected_text) if not hasattr(wrapper, "_base_nlp") else None
        if baseline is None:
            base_nlp = wrapper._base_nlp
            saved = legacy._NLP
            legacy._NLP = base_nlp
            try:
                baseline = legacy.decompose_sentence(expected_text)
            finally:
                legacy._NLP = saved

        token = semantic_frames._PROJECTED_DOCS.set({expected_text: projected_sentence.doc})
        try:
            reused = semantic_frames.decompose_sentence(expected_text)
        finally:
            semantic_frames._PROJECTED_DOCS.reset(token)

        assert _signature(reused) == _signature(baseline)
