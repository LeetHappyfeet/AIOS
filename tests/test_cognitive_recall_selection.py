from aios_app.epistemic.relevance import select_recalled_cognition


def _item(pid, text, score, kind="BELIEF", **extra):
    return {
        "proposition_id": pid,
        "text": text,
        "subject_norm": extra.pop("subject_norm", "shego"),
        "predicate_norm": extra.pop("predicate_norm", "remember"),
        "object_norm": extra.pop("object_norm", text),
        "claim_kind": kind,
        "effective_confidence": extra.pop("effective_confidence", 0.8),
        "relevance": {"total": score},
        **extra,
    }


def test_recall_competition_filters_low_information_unrelated_belief():
    good = _item("p1", "Shego worked with Alex", 1.6, kind="EVENT")
    junk = _item(
        "p2", "which be annoying", 1.4,
        subject_norm="which", predicate_norm="be", object_norm="annoying",
    )
    selected, suppressed = select_recalled_cognition(
        [junk, good], focus_text="Mia asks Shego what is new after working with Alex"
    )
    assert [row["proposition_id"] for row in selected] == ["p1"]
    assert suppressed["below_recall_floor"] == 1


def test_current_message_cognition_bypasses_historical_recall_floor():
    current = _item("c1", "Mia just arrived", 0.1, kind="EVENT", cognitive_commit=True)
    selected, _ = select_recalled_cognition([current], focus_text="")
    assert selected == [current]


def test_recall_competition_deduplicates_near_identical_candidates():
    first = _item("p1", "Shego worked with Alex on the computer", 2.0, kind="EVENT")
    second = _item("p2", "Shego worked with Alex on computer", 1.9, kind="EVENT")
    selected, suppressed = select_recalled_cognition(
        [first, second], focus_text="Shego Alex computer"
    )
    assert len(selected) == 1
    assert suppressed["redundant_recall"] == 1
