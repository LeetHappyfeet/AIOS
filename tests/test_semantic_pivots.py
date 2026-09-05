from aios_app.epistemic.pivots import (
    CHARACTER_IDENTITY_RULESET,
    resolve_subject_pivot,
)


def test_character_id_is_authoritative_first_person_pivot():
    pivot = resolve_subject_pivot(
        "I",
        character_id="alice",
        speaker_id="human-controller",
        speaker_role="user",
        recipient_id="bob",
        viewpoint_id="temporary-viewpoint",
    )
    assert pivot.resolved is True
    assert pivot.subject == "alice"
    assert pivot.character_id == "alice"
    assert pivot.memory_owner_id == "alice"
    assert pivot.viewpoint_id == "alice"
    assert pivot.epistemic_scope == "character"
    assert pivot.ruleset_id == CHARACTER_IDENTITY_RULESET


def test_character_id_owns_named_subject_memory_without_rewriting_subject():
    pivot = resolve_subject_pivot(
        "Charles",
        character_id="alice",
        speaker_id="alice-voice",
        speaker_role="character",
        recipient_id="bob",
    )
    assert pivot.resolved is False
    assert pivot.subject == "Charles"
    assert pivot.memory_owner_id == "alice"
    assert pivot.viewpoint_id == "alice"
    assert pivot.epistemic_scope == "character"


def test_first_person_falls_back_only_without_character_id():
    pivot = resolve_subject_pivot(
        "myself",
        character_id=None,
        speaker_id="agent:alice",
        speaker_role="agent",
        recipient_id=None,
    )
    assert pivot.resolved is True
    assert pivot.subject == "agent:alice"
    assert pivot.memory_owner_id is None
    assert pivot.epistemic_scope == "speaker"


def test_first_person_without_any_identity_is_not_guessed():
    pivot = resolve_subject_pivot(
        "I",
        character_id=None,
        speaker_id=None,
        speaker_role="character",
        recipient_id=None,
    )
    assert pivot.resolved is False
    assert pivot.subject == "I"
    assert pivot.epistemic_scope == "source"


def test_second_person_uses_recipient_but_character_still_owns_memory():
    pivot = resolve_subject_pivot(
        "you",
        character_id="alice",
        speaker_id="alice",
        speaker_role="character",
        recipient_id="bob",
    )
    assert pivot.resolved is True
    assert pivot.subject == "bob"
    assert pivot.pivot_type == "second_person"
    assert pivot.memory_owner_id == "alice"
    assert pivot.viewpoint_id == "alice"
