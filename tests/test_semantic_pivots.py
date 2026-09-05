from aios_app.epistemic.pivots import resolve_subject_pivot


def test_first_person_resolves_to_character_speaker():
    pivot = resolve_subject_pivot(
        "I",
        speaker_id="alice",
        speaker_role="character",
        recipient_id="bob",
    )
    assert pivot.resolved is True
    assert pivot.subject == "alice"
    assert pivot.pivot_type == "first_person"
    assert pivot.epistemic_scope == "character"
    assert pivot.viewpoint_id == "alice"


def test_first_person_resolves_agent_as_character_scope():
    pivot = resolve_subject_pivot(
        "myself",
        speaker_id="agent:alice",
        speaker_role="agent",
        recipient_id=None,
    )
    assert pivot.resolved is True
    assert pivot.subject == "agent:alice"
    assert pivot.epistemic_scope == "character"


def test_first_person_without_identity_is_not_guessed():
    pivot = resolve_subject_pivot(
        "I",
        speaker_id=None,
        speaker_role="character",
        recipient_id=None,
    )
    assert pivot.resolved is False
    assert pivot.subject == "I"
    assert pivot.epistemic_scope == "source"


def test_second_person_requires_explicit_recipient():
    pivot = resolve_subject_pivot(
        "you",
        speaker_id="alice",
        speaker_role="character",
        recipient_id="bob",
    )
    assert pivot.resolved is True
    assert pivot.subject == "bob"
    assert pivot.pivot_type == "second_person"


def test_named_subject_is_preserved():
    pivot = resolve_subject_pivot(
        "Charles",
        speaker_id="alice",
        speaker_role="character",
        recipient_id="bob",
    )
    assert pivot.resolved is False
    assert pivot.subject == "Charles"
    assert pivot.viewpoint_id == "alice"
