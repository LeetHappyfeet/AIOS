from aios_app.epistemic.semantic_frames import decompose_sentence


def test_have_no_idea_decomposes_epistemic_and_event():
    frames = decompose_sentence("Mark had no idea why Nina kept ringing but it probably wasn't good.")
    assert frames
    assert any(f.predicate_canonical == "know" and f.polarity == -1 for f in frames)
    assert any(f.predicate_canonical in {"ring", "keep"} for f in frames)


def test_temporal_sentence_retains_multiple_clause_frames():
    frames = decompose_sentence("But only temporarily, since it began again just as soon as it went dark.")
    assert len(frames) >= 2
    assert any(f.predicate_canonical in {"begin", "go"} for f in frames)
