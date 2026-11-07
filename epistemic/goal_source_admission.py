"""Deterministic, source-bound admission for candidate managed goals.

The parser is allowed to discover possible intention language, but source
authorship and executive eligibility are independent decisions. No inference
is called here; uncertain candidates remain in the cognition diagnostics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ADMISSION_VERSION = "goal-source-admission-v4"

# Narrator interpretations containing an embedded first-person construction
# are not assertions made by that grammatical first person.
_FIGURATIVE_LEAD = re.compile(
    r"\b(?:gesture|glance|look|expression|silence|movement|relocation|"
    r"posture|smile|nod|shrug|eyes?|face|tail|body language)\b"
    r".{0,150}?\b(?:(?:that|which)\s+)?"
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
_HYPOTHETICAL_LEAD = re.compile(
    r"\b(?:perhaps|maybe|possibly|hypothetically|if\s+only)\b.{0,90}$",
    re.I | re.S,
)
# A neuter pronoun has no verified character antecedent. Future-tense
# fragments inside noun phrases likewise do not establish an adopted action.
_UNRESOLVED_GOAL_SUBJECT = re.compile(
    r"\bit\s+(?:(?:does\s+not|doesn't|no\s+longer)\s+)?"
    r"(?:wants?|wanted|needs?|needed|intends?|intended|plans?|planned|"
    r"seeks?|sought|decides?|decided|resolves?|resolved|prepares?|prepared)\s+", re.I,
)
_SELF_FUTURE = re.compile(r"\bI(?:'ll|'m\s+going\s+to|\s+(?:will|am\s+going\s+to))\s+", re.I)
_INDEPENDENT_LEAD = re.compile(
    r"""(?:(?:^|[.!?;,:—]|\b(?:and|but|so)\b)\s*["'“”‘’]?"""
    r"""|\b(?:said|says|replied)\s*["'“‘])\s*"""
    r"(?:(?:then|now|soon|eventually|finally|actually|today|tomorrow|tonight|"
    r"next\s+\w+|after\s+\w+)\s+)?$", re.I,
)
_BARE_REFERENCE = re.compile(
    r"\b(?:it|them)\b|\b(?:this|that|these|those|one|ones)\b"
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
_SHORT_ERRAND_ACTION = re.compile(
    r"^(?:to\s+)?(?:get|fetch|grab|pour|drink)\s+"
    r"(?:(?:a|some|the|my|your)\s+)?(?:glass|cup|bottle|water|coffee|tea|drink|snack|pizza)\b", re.I,
)
_LOCATIVE_ACTION = re.compile(
    r"^(?:to\s+)?be\s+(?:over\s+)?(?:here|there|nearby)\b", re.I,
)
_EXTENDED_TIME = re.compile(
    r"\b(?:tomorrow|next\s+(?:week|month|year)|"
    r"(?:two|three|four|five|six|seven|\d+)\s+"
    r"(?:nights?|days?|weeks?|months?|years?))\b", re.I,
)

# These constructions describe the delivery of the current utterance, rather
# than an objective that should survive it. A future communication task such as
# "tell Mia the results tomorrow" is deliberately not excluded.
_DISCOURSE_ACTION = re.compile(
    r"^(?:to\s+)?(?:say|tell|mention|repeat|ask)\b.*\b(?:once|again|this|that)\b|"
    r"^(?:to\s+)?(?:have\s+to|get\s+to)\s+(?:watch|listen|hear|say)\b|"
    r"^(?:to\s+)?(?:be\s+)?on\s+the\s+record\b", re.I,
)
_INCOMPLETE_OBJECTIVE = re.compile(
    r"^(?:to\s+)?(?:was|were|is|are)\b|^(?:to\s+)?(?:a|an|the)\s*$|"
    r"\b(?:how|what|where|whether)\s*$", re.I,
)

# These are candidate desires/positions, not sufficiently specified executive
# objectives. Preserve them in source/audit rather than inventing a task.
_INDEFINITE_OUTCOME = re.compile(r"\b(?:somewhere|something|anything|whatever)\b", re.I)
_ABSTRACT_STATE = re.compile(r"^(?:to\s+)?(?:be|feel)\s+(?!able\s+to\b)", re.I)
_FIGURATIVE_OBJECTIVE = re.compile(r"\bdie\s+on\s+(?:(?:this|that|the|a|my)\s+)?hill\b", re.I)
_COMMUNICATION = re.compile(r"^(?:to\s+)?(?:say|tell|mention|repeat|ask)\b", re.I)
_EMBEDDED_TOPIC = re.compile(
    r"\b(?:whatever|what\s+(?:I|you|we|they)(?:'d|\s+would)?\s+"
    r"(?:really\s+)?(?:like|want|need))\b", re.I,
)


@dataclass(frozen=True)
class GoalAdmission:
    decision: str
    reason: str
    source_form: str

    @property
    def managed(self) -> bool:
        return self.decision == "admit"


def _action_text(value: str) -> str:
    return " ".join(re.sub(r"[*_`]+", "", value).split())


def _source_head(
    pattern: re.Pattern[str], source: str, action: str, match_span: tuple[int, int] | None,
) -> re.Match[str] | None:
    if match_span is not None:
        return pattern.match(source, match_span[0])
    # Older receipts and enriched excerpts may not carry a span. Match the
    # action as well so a separate commitment in the excerpt cannot decide it.
    objective = re.sub(r"^to\s+", "", action.casefold())
    for head in pattern.finditer(source):
        tail = re.sub(r"^to\s+", "", _action_text(source[head.end():]).casefold())
        if re.match(re.escape(objective) + r"(?!\w)", tail):
            return head
    return None


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
    # Markdown emphasis is presentation, not an escape from source review.
    action = _action_text(str(objective or ""))
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
    if _HYPOTHETICAL_LEAD.search(prefix):
        return GoalAdmission("insufficient_commitment", "hypothetical_intention", "source")
    if not action.strip():
        return GoalAdmission("insufficient_commitment", "empty_objective", "unknown")
    if _source_head(_UNRESOLVED_GOAL_SUBJECT, source, action, match_span):
        return GoalAdmission("attribution_unresolved", "unresolved_goal_subject", "source")
    future = _source_head(_SELF_FUTURE, source, action, match_span)
    if future and not _INDEPENDENT_LEAD.search(re.sub(r"[*_`]+", "", source[:future.start()])):
        return GoalAdmission("insufficient_commitment", "embedded_future_clause", "source")
    if re.match(r"(?i)^no\s+", action):
        return GoalAdmission("insufficient_commitment", "constraint_without_action", "source")
    if _INCOMPLETE_OBJECTIVE.search(action):
        return GoalAdmission("insufficient_commitment", "incomplete_objective", "source")
    if _FIGURATIVE_OBJECTIVE.search(action):
        return GoalAdmission("nonliteral_statement", "figurative_objective", "source")
    if _COMMUNICATION.search(action) and _EMBEDDED_TOPIC.search(action):
        return GoalAdmission("insufficient_commitment", "unresolved_communication_topic", "source")
    if _DISCOURSE_ACTION.search(action) and not _EXTENDED_TIME.search(source):
        return GoalAdmission("scene_only", "current_utterance_or_reaction", "source")
    if _BARE_REFERENCE.search(action):
        return GoalAdmission("unresolved_reference",
                             "objective_contains_unresolved_reference", "source")
    if str(horizon or "").lower() == "immediate":
        return GoalAdmission("scene_only", "immediate_intention", "source")
    if ((_SCENE_ACTION.search(action) or _SHORT_ERRAND_ACTION.search(action)
         or _LOCATIVE_ACTION.search(action))
            and (_NEAR_TIME.search(source) or not _EXTENDED_TIME.search(source))):
        return GoalAdmission("scene_only", "transient_scene_action", "source")
    if _INDEFINITE_OUTCOME.search(action) or _ABSTRACT_STATE.search(action):
        return GoalAdmission("insufficient_commitment", "unspecified_desired_outcome", "desire")
    # An objective must at least name an action and its complement. Bare
    # intention words are candidates for later review, not executive authority.
    words = re.findall(r"[A-Za-z]+", re.sub(r"^to\s+", "", action, flags=re.I))
    if len(words) < 2:
        return GoalAdmission("insufficient_commitment", "incomplete_objective", "source")
    return GoalAdmission("admit", "source_grounded_managed_intention", "source")
