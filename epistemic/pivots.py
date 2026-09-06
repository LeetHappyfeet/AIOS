from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


FIRST_PERSON_SUBJECTS = {"i", "me", "myself"}
SECOND_PERSON_SUBJECTS = {"you", "yourself"}
CHARACTER_IDENTITY_RULESET = "character-id-v1"


@dataclass(frozen=True)
class PivotResolution:
    subject: Optional[str]
    pivot_type: Optional[str]
    epistemic_scope: str
    character_id: Optional[str]
    viewpoint_id: Optional[str]
    memory_owner_id: Optional[str]
    recipient_id: Optional[str]
    ruleset_id: str
    resolved: bool

    def as_meta(self) -> dict:
        return {
            "pivot_type": self.pivot_type,
            "epistemic_scope": self.epistemic_scope,
            "character_id": self.character_id,
            "viewpoint_id": self.viewpoint_id,
            "memory_owner_id": self.memory_owner_id,
            "recipient_id": self.recipient_id,
            "identity_ruleset": self.ruleset_id,
            "resolved": self.resolved,
        }


def _norm(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def resolve_subject_pivot(
    subject: Optional[str],
    *,
    character_id: Optional[str],
    speaker_id: Optional[str],
    speaker_role: Optional[str],
    recipient_id: Optional[str],
    viewpoint_id: Optional[str] = None,
    ruleset_id: str = CHARACTER_IDENTITY_RULESET,
) -> PivotResolution:
    """
    Resolve deterministic identity pivots for a character-scoped memory chain.

    Core invariant:
        first-person language resolves to the already-resolved viewpoint_id.
        character_id is the active character context, not automatically the
        physical speaker or first-person identity.

    speaker_id remains provenance: who supplied the text. A character memory
    owner exists only when the effective viewpoint is that character; a human
    using a character as an augmentation/search context must not silently write
    first-person statements into the character memory chain.

    Named subjects are never rewritten merely because the chain has a
    character owner. The owner controls epistemic/RDF scope, not proposition
    subject identity.
    """
    clean_subject = _norm(subject)
    key = clean_subject.lower() if clean_subject else None
    character = _norm(character_id)
    speaker = _norm(speaker_id)
    explicit_viewpoint = _norm(viewpoint_id)
    recipient = _norm(recipient_id)

    effective_viewpoint = explicit_viewpoint or (
        (speaker or character) if speaker_role == "character" else speaker
    )
    first_person_identity = effective_viewpoint
    memory_owner = (
        character
        if character and effective_viewpoint == character
        else None
    )

    if key in FIRST_PERSON_SUBJECTS and first_person_identity:
        return PivotResolution(
            subject=first_person_identity,
            pivot_type="first_person",
            epistemic_scope=(
                "character"
                if memory_owner
                else ("speaker" if effective_viewpoint else "source")
            ),
            character_id=character,
            viewpoint_id=effective_viewpoint,
            memory_owner_id=memory_owner,
            recipient_id=recipient,
            ruleset_id=ruleset_id,
            resolved=True,
        )

    if key in SECOND_PERSON_SUBJECTS and recipient:
        return PivotResolution(
            subject=recipient,
            pivot_type="second_person",
            epistemic_scope=(
                "character"
                if memory_owner
                else ("speaker" if effective_viewpoint else "recipient")
            ),
            character_id=character,
            viewpoint_id=effective_viewpoint,
            memory_owner_id=memory_owner,
            recipient_id=recipient,
            ruleset_id=ruleset_id,
            resolved=True,
        )

    return PivotResolution(
        subject=clean_subject,
        pivot_type=None,
        epistemic_scope=(
            "character"
            if memory_owner
            else ("speaker" if effective_viewpoint else "source")
        ),
        character_id=character,
        viewpoint_id=effective_viewpoint,
        memory_owner_id=memory_owner,
        recipient_id=recipient,
        ruleset_id=ruleset_id,
        resolved=False,
    )
