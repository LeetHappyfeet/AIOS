"""Source-only review for planning-formulated goals.

A language model may select an existing author-owned intention, but cannot
create a new objective through paraphrase, lexical overlap, or narrative context.
This module has no dependency on semantic topology, vector/RDF projections or
inference. Original V11 admission remains the candidate authority.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_NOISE = frozenset({
    "character", "intends", "intend", "wants", "want", "needs",
    "need", "plans", "plan", "will", "going", "goal", "objective", "that",
    "this", "them", "their", "they", "with", "have", "from", "into",
    "about", "some", "would", "should", "could", "might", "the", "and",
    "for", "are",
})


def _terms(value: str, *, owner: str = "") -> set[str]:
    noise = _NOISE | {t for t in re.findall(r"[a-z]{3,}", owner.casefold())}
    return set(re.findall(r"[a-z]{3,}", str(value).casefold())) - noise


@dataclass(frozen=True)
class FormationReview:
    decision: str
    reason: str
    unit: Any | None = None

    @property
    def eligible(self) -> bool:
        return self.decision == "eligible" and self.unit is not None


def verify_formed_goal(
    *, source_text: str, proposal: str, character_id: str,
    speaker_id: str | None, speaker_role: str | None,
) -> FormationReview:
    # Import lazily: message cognition already imports the source-admission
    # policy, but the inference operation is separate from source ingestion.
    from aios_app.epistemic.message_cognition import interpret_message
    if not source_text or not proposal:
        return FormationReview("reject", "missing_source_or_proposal")
    if speaker_role not in {"character", "assistant"}:
        return FormationReview("reject", "foreign_speaker_role")
    units = interpret_message(
        source_text, character_id=character_id, speaker_id=speaker_id,
        speaker_role=speaker_role, viewpoint_id=speaker_id,
    )
    positive = [u for u in units if u.claim_kind == "GOAL" and
                u.polarity > 0 and u.meta.get("character_owned") and
                u.meta.get("goal_admission_status") == "admit"]
    if not positive:
        return FormationReview("reject", "no_character_owned_source_commitment")
    proposed_terms = _terms(proposal, owner=character_id)
    matches = []
    for unit in positive:
        objective_terms = _terms(str(unit.meta.get("objective") or ""), owner=character_id)
        # The model cannot introduce even one new substantive term. A short
        # objective that drops an essential source term is also not equivalent.
        if objective_terms and proposed_terms == objective_terms:
            matches.append(unit)
    if len(matches) != 1:
        return FormationReview("defer", "proposal_not_unique_source_objective")
    return FormationReview("eligible", "verified_positive_source_unit", matches[0])
