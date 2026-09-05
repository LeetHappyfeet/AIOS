from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


FIRST_PERSON_SUBJECTS = {"i", "me", "myself"}
SECOND_PERSON_SUBJECTS = {"you", "yourself"}


@dataclass(frozen=True)
class PivotResolution:
    subject: Optional[str]
    pivot_type: Optional[str]
    epistemic_scope: str
    viewpoint_id: Optional[str]
    recipient_id: Optional[str]
    resolved: bool

    def as_meta(self) -> dict:
        return {
            "pivot_type": self.pivot_type,
            "epistemic_scope": self.epistemic_scope,
            "viewpoint_id": self.viewpoint_id,
            "recipient_id": self.recipient_id,
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
    speaker_id: Optional[str],
    speaker_role: Optional[str],
    recipient_id: Optional[str],
) -> PivotResolution:
    """
    Resolve only high-confidence deictic pivots during deterministic ingestion.

    "I" is an epistemic boundary: when the source has an identified speaker,
    first-person assertions are rewritten to that speaker's stable identifier
    instead of remaining a context-dependent pronoun.

    Second person is resolved only when an explicit recipient exists. All other
    language is preserved for later semantic resolution rather than guessed.
    """
    clean_subject = _norm(subject)
    key = clean_subject.lower() if clean_subject else None
    speaker = _norm(speaker_id)
    recipient = _norm(recipient_id)

    if key in FIRST_PERSON_SUBJECTS and speaker:
        scope = "character" if speaker_role in {"character", "agent"} else "speaker"
        return PivotResolution(
            subject=speaker,
            pivot_type="first_person",
            epistemic_scope=scope,
            viewpoint_id=speaker,
            recipient_id=recipient,
            resolved=True,
        )

    if key in SECOND_PERSON_SUBJECTS and recipient:
        return PivotResolution(
            subject=recipient,
            pivot_type="second_person",
            epistemic_scope="recipient",
            viewpoint_id=speaker,
            recipient_id=recipient,
            resolved=True,
        )

    return PivotResolution(
        subject=clean_subject,
        pivot_type=None,
        epistemic_scope="source",
        viewpoint_id=speaker,
        recipient_id=recipient,
        resolved=False,
    )
