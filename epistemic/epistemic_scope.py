from __future__ import annotations

"""Shared lightweight epistemic-scope policy.

The fast message-cognition path and the richer semantic-frame path must agree
about whether language is asserted, hypothetical, conditional, or
counterfactual. This module intentionally uses conservative lexical cues: a
false negative merely leaves work for the structural semantic pipeline, while
a false positive could incorrectly promote imagined content into memory.
"""

import re
from dataclasses import replace
from typing import Callable, Iterable

SCOPE_ASSERTED = "asserted"
SCOPE_HYPOTHETICAL = "hypothetical"
SCOPE_CONDITIONAL = "conditional"
SCOPE_COUNTERFACTUAL = "counterfactual"
SCOPE_QUESTION = "question"
NONASSERTIVE_SCOPES = {
    SCOPE_HYPOTHETICAL,
    SCOPE_CONDITIONAL,
    SCOPE_COUNTERFACTUAL,
    SCOPE_QUESTION,
}

_HYPOTHETICAL_RE = re.compile(
    r"^\s*(?:let(?:'|’)s\s+(?:say|suppose|imagine)|suppose|supposing|imagine|assuming)\b",
    re.I,
)
_CONDITIONAL_RE = re.compile(
    r"^\s*(?:if|provided\s+that|assuming\s+that|on\s+condition\s+that)\b",
    re.I,
)
_COUNTERFACTUAL_RE = re.compile(
    r"^\s*(?:if\s+only|i\s+wish|wish\s+that)\b",
    re.I,
)
_QUESTION_RE = re.compile(r"\?\s*[\"'”’\)\]]*\s*$")

# Strong punctuation can join two independent clauses into one spaCy sentence.
# Only split when the right side begins like an explicit finite clause. This
# keeps ordinary parenthetical/emphatic dashes intact.
_STRONG_BOUNDARY_RE = re.compile(r"\s*(?:—|;|\s--\s)\s*")
_INDEPENDENT_RIGHT_RE = re.compile(
    r"^(?:[\"'“‘]*)\s*(?:I|you|he|she|we|they|it|[A-Z][A-Za-z0-9_-]*)\s+"
    r"(?:am|is|are|was|were|have|has|had|do|does|did|can|could|will|would|shall|should|may|might|must|"
    r"[A-Za-z]+(?:s|ed))\b",
    re.I,
)


def classify_scope(text: str) -> str:
    clean = (text or "").strip()
    if not clean:
        return SCOPE_ASSERTED
    if _QUESTION_RE.search(clean):
        return SCOPE_QUESTION
    if _COUNTERFACTUAL_RE.search(clean):
        return SCOPE_COUNTERFACTUAL
    if _HYPOTHETICAL_RE.search(clean):
        return SCOPE_HYPOTHETICAL
    if _CONDITIONAL_RE.search(clean):
        return SCOPE_CONDITIONAL
    return SCOPE_ASSERTED


def split_strong_clauses(text: str) -> list[str]:
    """Split only punctuation boundaries with an independent right clause."""
    remaining = (text or "").strip()
    if not remaining:
        return []
    parts: list[str] = []
    while True:
        match = _STRONG_BOUNDARY_RE.search(remaining)
        if match is None:
            break
        right = remaining[match.end():].strip()
        if not right or not _INDEPENDENT_RIGHT_RE.search(right):
            # Look for a later strong boundary rather than destructively
            # splitting a parenthetical dash.
            later = _STRONG_BOUNDARY_RE.search(remaining, match.end())
            if later is None:
                break
            match = later
            right = remaining[match.end():].strip()
            if not right or not _INDEPENDENT_RIGHT_RE.search(right):
                break
        left = remaining[:match.start()].strip()
        if left:
            parts.append(left)
        remaining = right
    if remaining:
        parts.append(remaining)
    return parts


def effective_scope(text: str, inherited: str = SCOPE_ASSERTED) -> str:
    local = classify_scope(text)
    return inherited if local == SCOPE_ASSERTED and inherited != SCOPE_ASSERTED else local


def install_message_cognition_scope_guard(module) -> None:
    """Upgrade message cognition in place without duplicating its DB writer.

    commit_message_cognition resolves INTERPRETER_VERSION and interpret_message
    from its defining module at call time, so replacing those globals keeps the
    existing idempotent commit/reconciliation implementation while hardening
    only linguistic admission.
    """
    if getattr(module, "_epistemic_scope_guard_installed", False):
        return

    original: Callable = module.interpret_message

    def guarded_interpret_message(
        text: str,
        *,
        character_id: str,
        speaker_id: str | None,
        speaker_role: str | None,
        viewpoint_id: str | None,
    ):
        units = []
        for sentence in module._sentences(text):
            sentence_scope = classify_scope(sentence)
            for clause in split_strong_clauses(sentence):
                scope = effective_scope(clause, sentence_scope)
                if scope in NONASSERTIVE_SCOPES:
                    continue
                clause_units = original(
                    clause,
                    character_id=character_id,
                    speaker_id=speaker_id,
                    speaker_role=speaker_role,
                    viewpoint_id=viewpoint_id,
                )
                for unit in clause_units:
                    meta = dict(unit.meta)
                    meta.update({
                        "epistemic_scope_policy": "epistemic-scope-v1",
                        "effective_modality": scope,
                    })
                    units.append(replace(unit, meta=meta))
        # Preserve the fast path's hard bound after clause expansion.
        return units[: module.MAX_UNITS]

    module.INTERPRETER_VERSION = "message-cognition-v4"
    module.interpret_message = guarded_interpret_message
    module._epistemic_scope_guard_installed = True
