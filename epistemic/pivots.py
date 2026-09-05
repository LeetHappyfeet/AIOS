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
        if character_id exists, first-person language ("I", "me", "myself")
        resolves to character_id.

    speaker_id remains provenance: who supplied the text. viewpoint_id can
    describe an explicit controller/narrator perspective, but neither is
    allowed to displace character_id as the first-person root of a character
    memory chain.

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

    # character_id is authoritative for character memory. Fall back only for
    # non-character sources/legacy data where no character binding exists.
    first_person_identity = character or explicit_viewpoint or speaker
    memory_owner = character
    effective_viewpoint = character or explicit_viewpoint or speaker

    if key in FIRST_PERSON_SUBJECTS and first_person_identity:
        return PivotResolution(
            subject=first_person_identity,
            pivot_type="first_person",
            epistemic_scope="character" if character else "speaker",
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
            epistemic_scope="character" if character else "recipient",
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
        epistemic_scope="character" if character else "source",
        character_id=character,
        viewpoint_id=effective_viewpoint,
        memory_owner_id=memory_owner,
        recipient_id=recipient,
        ruleset_id=ruleset_id,
        resolved=False,
    )
