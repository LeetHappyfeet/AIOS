"""Deterministic, source-bound admission for candidate managed goals.

The parser is allowed to discover possible intention language, but source
authorship and executive eligibility are independent decisions. No inference
is called here; uncertain candidates remain in the cognition diagnostics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ADMISSION_VERSION = "goal-source-admission-v1"

# Narrator interpretations containing an embedded first-person construction
# are not assertions made by that grammatical first person.
_FIGURATIVE_LEAD = re.compile(
    r"\b(?:gesture|glance|look|expression|silence|movement|relocation|"
    r"posture|smile|nod|shrug|eyes?|face|tail|body language)\b"
    r".{0,150}?\b(?:that|which)\s+"
    r"(?:said|suggested|implied|seemed\s+to\s+say|might\s+have\s+said)\b",
    re.I | re.S,
)
_NONLITERAL_LEAD = re.compile(
    r"\b(?:as\s+if|as\s+though|imagined\s+saying|thought\s+about\s+saying|"
    r"would\s+have\s+said|in\s+(?:her|his|their)\s+head)\b", re.I,
)
_AFTER_EMBEDDED_SPEECH = re.compile(
    r"\b(?:she|he|they|the\s+character)\s+"
    r"(?:imagined|thought\s+about|considered|pretended)\s+"
    r"(?:saying|telling|announcing)\b", re.I,
)
_BARE_REFERENCE = re.compile(
    r"\b(?:it|them|this|that|these|those|one|ones)\b"
    r"(?=\s*(?:$|[,.!?;:]|\b(?:in|on|over|under|down|up|away|out|back)\b))",
    re.I,
)
_NEAR_TIME = re.compile(
    r"\b(?:in\s+(?:a|one|two|three|\d+)\s+(?:minute|minutes|second|seconds)|"
    r"right\s+now|immediately|in\s+a\s+moment)\b", re.I,
)
_SCENE_ACTION = re.compile(
    r"^(?:to\s+|be\s+)?(?:put|putting|set|setting|place|placing|"
    r"pick|picking|move|moving|sit|sitting|stand|standing|"
    r"turn|turning|look|looking|reach|reaching)\b", re.I,
)
_EXTENDED_TIME = re.compile(
    r"\b(?:tomorrow|next\s+(?:week|month|year)|"
    r"(?:two|three|four|five|six|seven|\d+)\s+"
    r"(?:nights?|days?|weeks?|months?|years?))\b", re.I,
)


@dataclass(frozen=True)
class GoalAdmission:
    decision: str
    reason: str
    source_form: str

    @property
    def managed(self) -> bool:
        return self.decision == "admit"


def review_goal_source(
    *, source_text: str, objective: str,
    parse_reason: str | None = None, horizon: str | None = None,
    match_span: tuple[int, int] | None = None,
) -> GoalAdmission:
    """Conservative evidence-only review: never invent missing antecedents.

    Source is the original clause, not the canonical GOAL rewrite. Its role
    identity is checked separately by message cognition. A match span keeps
    preceding narrator language available to the attribution review.
    """
    source = str(source_text or "").replace("’", "'")
    action = " ".join(str(objective or "").split())
    if not source.strip():
        return GoalAdmission("attribution_unresolved", "missing_source_text", "unknown")
    prefix = source[:match_span[0]] if match_span is not None else source
    suffix = source[match_span[1]:] if match_span is not None else source
    if (_FIGURATIVE_LEAD.search(prefix)
            or _NONLITERAL_LEAD.search(prefix)
            or _AFTER_EMBEDDED_SPEECH.search(suffix)
            or _AFTER_EMBEDDED_SPEECH.search(source)):
        return GoalAdmission("nonliteral_statement",
                             "narrator_implied_or_imagined_first_person", "narration")
    if not action.strip():
        return GoalAdmission("insufficient_commitment", "empty_objective", "unknown")
    if _BARE_REFERENCE.search(action):
        return GoalAdmission("unresolved_reference",
                             "objective_contains_unresolved_reference", "source")
    if str(horizon or "").lower() == "immediate":
        return GoalAdmission("scene_only", "immediate_intention", "source")
    if (_SCENE_ACTION.search(action)
            and (_NEAR_TIME.search(source) or not _EXTENDED_TIME.search(source))):
        return GoalAdmission("scene_only", "transient_scene_action", "source")
    return GoalAdmission("admit", "source_grounded_managed_intention", "source")
