from aios_app.epistemic.semantic_frames import decompose_sentence


def _by_predicate(frames, predicate):
    return [frame for frame in frames if frame.predicate_canonical == predicate]


def test_direct_quote_gets_local_speaker_perspective():
    frames = decompose_sentence('Alice says, "I saw Bob."')
    say = _by_predicate(frames, "say")[0]
    see = _by_predicate(frames, "see")[0]

    assert say.subject == "Alice"
    assert see.meta["inside_direct_quote"] is True
    assert see.meta["perspective_kind"] == "quoted_speech"
    assert see.meta["perspective_holder_text"] == "Alice"
    assert see.meta["perspective_source_index"] == say.index
    assert see.discourse_mode == "attributed_speech_content"


def test_indirect_report_is_not_treated_as_direct_quote():
    frames = decompose_sentence("Alice says Bob left.")
    leave = _by_predicate(frames, "leave")[0]

    assert leave.meta["inside_direct_quote"] is False
    assert leave.meta["perspective_kind"] == "attributed_speech_content"
    assert leave.meta["perspective_holder_text"] == "Alice"


def test_nested_belief_replaces_local_holder_without_losing_chain():
    frames = decompose_sentence("Alice says Bob believes Carol lied.")
    believe = _by_predicate(frames, "believe")[0]
    lie = _by_predicate(frames, "lie")[0]

    assert believe.meta["perspective_holder_text"] == "Alice"
    assert lie.meta["perspective_kind"] == "mental_content"
    assert lie.meta["perspective_holder_text"] == "Bob"
    assert lie.meta["perspective_depth"] >= 2
    assert lie.meta["perspective_chain"][-1]["holder"] == "Bob"
    assert lie.discourse_mode == "attributed_mental_content"


def test_plain_narration_has_no_local_perspective_holder():
    frames = decompose_sentence("Carol opened the door.")
    open_frame = _by_predicate(frames, "open")[0]

    assert open_frame.meta["perspective_kind"] is None
    assert open_frame.meta["perspective_holder_text"] is None
    assert open_frame.discourse_mode == "narrated_observation"
