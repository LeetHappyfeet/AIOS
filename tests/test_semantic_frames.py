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


def test_nominal_coordination_does_not_create_fake_predicates():
    frames = decompose_sentence(
        "With piercing pink glowing eyes and her sensitive ears she noticed your arrival and she turned to you."
    )
    predicates = {f.predicate_canonical for f in frames}
    assert "ear" not in predicates
    assert "eye" not in predicates
    assert "notice" in predicates
    assert "turn" in predicates


def test_root_frame_is_labeled_main():
    frames = decompose_sentence("She walks to you and she smirks.")
    assert any(f.frame_role == "main" and f.predicate_canonical == "walk" for f in frames)


def test_relative_pronoun_resolves_to_local_head():
    frames = decompose_sentence("The man who was shot dead by federal agents was identified.")
    shot = next(f for f in frames if f.predicate_canonical == "shoot")
    assert shot.subject is not None
    assert "man" in shot.subject.lower()
    assert shot.subject.lower() != "who"


def test_passive_reporting_does_not_make_theme_the_believer():
    frames = decompose_sentence("Pretti is believed to be an American citizen.")
    belief = next(f for f in frames if f.predicate_canonical == "believe")
    child = next(f for f in frames if f.predicate_canonical == "be_definition_of")
    assert belief.subject is None
    assert belief.discourse_mode == "reported_claim"
    assert belief.object_frame_index == child.index
    assert child.subject is not None
    assert "pretti" in child.subject.lower()


def test_xcomp_subject_inheritance_does_not_loop():
    frames = decompose_sentence("Stop hurting people!")
    assert frames
    assert any(f.predicate_canonical == "stop" for f in frames)
    assert any(f.predicate_canonical == "hurt" for f in frames)



def test_modifier_clauses_are_not_semantic_object_frames():
    frames = decompose_sentence(
        "Alex almost got flagged because Alex waved at the drone."
    )
    flagged = next(f for f in frames if f.predicate_canonical == "flag")
    waved = next(f for f in frames if f.predicate_canonical == "wave")
    assert waved.parent_index == flagged.index
    assert flagged.object_frame_index is None


def test_relative_clause_is_not_semantic_object_frame():
    frames = decompose_sentence(
        "A green flame that stretched itself across the screen flickered."
    )
    flicker = next(f for f in frames if f.predicate_canonical == "flicker")
    stretch = next(f for f in frames if f.predicate_canonical == "stretch")
    assert stretch.frame_role == "relcl"
    assert flicker.object_frame_index is None


def test_explicit_object_survives_unrelated_child_clause():
    frames = decompose_sentence(
        "You ate a granola bar because you missed dinner."
    )
    ate = next(f for f in frames if f.predicate_canonical == "eat")
    assert ate.object_text is not None
    assert "granola bar" in ate.object_text.lower()
    assert ate.object_frame_index is None


def test_proposition_taking_predicate_keeps_nested_content():
    frames = decompose_sentence("Shego believes Alex stole the laptop.")
    belief = next(f for f in frames if f.predicate_canonical == "believe")
    stole = next(f for f in frames if f.predicate_canonical == "steal")
    assert belief.object_frame_index == stole.index



def test_phrase_prunes_entire_relative_clause_branch():
    frames = decompose_sentence(
        "A lazy, flickering flame that stretched itself into something vaguely woman-shaped appeared."
    )
    appeared = next(f for f in frames if f.predicate_canonical == "appear")
    stretch = next(f for f in frames if f.predicate_canonical == "stretch")
    assert appeared.subject is not None
    assert "flame" in appeared.subject.lower()
    assert "itself" not in appeared.subject.lower()
    assert "woman" not in appeared.subject.lower()
    assert stretch.subject is not None
    assert "flame" in stretch.subject.lower()
    assert "itself" not in stretch.subject.lower()


def test_local_antecedent_candidates_prefer_current_claim_order():
    from aios_app.epistemic.semantic_frames_legacy import (
        _choose_antecedent,
        _draft_antecedent_candidates,
    )

    frames = decompose_sentence(
        "A green ember bloomed before it resolved into a flame."
    )
    resolved = next(f for f in frames if f.predicate_canonical == "resolve")
    candidates = _draft_antecedent_candidates(frames, resolved.index)
    antecedent, _, confidence = _choose_antecedent(resolved.subject, candidates)
    assert antecedent is not None
    assert "ember" in antecedent.lower()
    assert confidence > 0.20
