from __future__ import annotations

"""Language-neutral semantic interpretation between syntax and epistemics."""

from dataclasses import dataclass, field
from typing import Mapping, Optional

SEMANTIC_INTERPRETER_VERSION = "semantic-interpreter-v2"

SEMANTIC_TYPE_TO_PREDICATE_FAMILY = {
    "ACTION": "ACTION",
    "CAUSE": "CAUSAL",
    "COMMUNICATION": "COMMUNICATION",
    "DESIRE": "GOAL",
    "DESCRIPTION": "DESCRIPTIVE",
    "IDENTITY": "IDENTITY",
    "INTENTION": "GOAL",
    "LOCATION": "SPATIAL",
    "MEMORY": "MEMORY",
    "MENTAL_STATE": "EPISTEMIC",
    "POSSESSION": "POSSESSION",
    "RELATION": "SOCIAL",
    "RULE": "RULE",
    "TEMPORAL": "TEMPORAL",
    "UNKNOWN": "UNKNOWN",
}

SEMANTIC_TYPE_TO_CLAIM_KIND = {
    "ACTION": "EVENT",
    "CAUSE": "EVENT",
    "COMMUNICATION": "EVENT",
    "DESIRE": "GOAL",
    "DESCRIPTION": "STATE",
    "IDENTITY": "STATE",
    "INTENTION": "GOAL",
    "LOCATION": "STATE",
    "MEMORY": "MEMORY",
    "MENTAL_STATE": "BELIEF",
    "POSSESSION": "STATE",
    "RELATION": "RELATIONSHIP",
    "RULE": "RULE",
    "TEMPORAL": "STATE",
    "UNKNOWN": "UNKNOWN",
}

# English lexical knowledge belongs to the source-language adapter boundary.
# It maps surface lemmas into stable semantic types; downstream epistemic code
# consumes those semantic types rather than the English words themselves.
_ENGLISH_LEMMA_HINTS = {
    "remember": "MEMORY", "recall": "MEMORY", "forget": "MEMORY",
    "know": "MENTAL_STATE", "believe": "MENTAL_STATE", "think": "MENTAL_STATE",
    "suspect": "MENTAL_STATE", "assume": "MENTAL_STATE", "infer": "MENTAL_STATE",
    "understand": "MENTAL_STATE", "want": "DESIRE", "wish": "DESIRE",
    "desire": "DESIRE", "intend": "INTENTION", "plan": "INTENTION",
    "try": "INTENTION", "attempt": "INTENTION", "seek": "INTENTION",
    "say": "COMMUNICATION", "tell": "COMMUNICATION", "ask": "COMMUNICATION",
    "reply": "COMMUNICATION", "report": "COMMUNICATION", "claim": "COMMUNICATION",
    "state": "COMMUNICATION", "write": "COMMUNICATION", "have": "POSSESSION",
    "own": "POSSESSION", "possess": "POSSESSION", "carry": "POSSESSION",
    "hold": "POSSESSION", "enter": "LOCATION", "leave": "LOCATION",
    "arrive": "LOCATION", "depart": "LOCATION", "cause": "CAUSE",
    "prevent": "CAUSE", "enable": "CAUSE", "seem": "DESCRIPTION",
    "appear": "DESCRIPTION", "look": "DESCRIPTION",
}

_IDENTITY_NOUNS = {
    "person", "human", "character", "unit", "machine", "android", "cyborg",
    "soldier", "officer", "doctor", "teacher", "student", "administrator",
    "admin", "partner", "assistant", "member", "agent",
}
_LOCATION_PREPOSITIONS = {"in", "at", "inside", "within", "on", "near"}
_DEPENDENT_ROLES = {"xcomp", "ccomp", "advcl", "acl", "relcl", "fallback"}


@dataclass(frozen=True)
class SemanticInterpretation:
    semantic_type: str
    confidence: float
    standalone_semantic: bool
    source_language: str = "en"
    interpreter_version: str = SEMANTIC_INTERPRETER_VERSION
    cues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def predicate_family(self) -> str:
        return SEMANTIC_TYPE_TO_PREDICATE_FAMILY.get(self.semantic_type, "UNKNOWN")

    @property
    def claim_kind(self) -> str:
        return SEMANTIC_TYPE_TO_CLAIM_KIND.get(self.semantic_type, "UNKNOWN")

    def as_meta(self) -> dict:
        return {
            "semantic_interpreter_version": self.interpreter_version,
            "semantic_type": self.semantic_type,
            "semantic_confidence": self.confidence,
            "standalone_semantic": self.standalone_semantic,
            "source_language": self.source_language,
            "semantic_cues": list(self.cues),
        }


def _norm(value: Optional[str]) -> str:
    return " ".join((value or "").strip().lower().split())


def _standalone(*, frame_role: Optional[str], subject: Optional[str], predicate: Optional[str], resolution_status: Optional[str]) -> bool:
    """Whether this semantic unit may stand independently in cognition.

    A root operator may be standalone even when its content is another frame:
    WANT(Ren, BUILD(...)) is a valid desire.  The BUILD xcomp is the dependent
    unit and remains non-atomic.  This keeps nested content structural without
    throwing away the parent belief/goal/communication act.
    """
    role = _norm(frame_role)
    if resolution_status and _norm(resolution_status) != "resolved":
        return False
    if not predicate or not subject:
        return False
    if role in _DEPENDENT_ROLES:
        return False
    return role in {"", "main", "root", "conj"}


def interpret_frame(*, predicate: Optional[str], predicate_surface: Optional[str] = None, subject: Optional[str] = None, object_value: Optional[str] = None, frame_role: Optional[str] = None, resolution_status: Optional[str] = None, object_frame_id: object | None = None, meta: Optional[Mapping[str, object]] = None, source_language: str = "en") -> SemanticInterpretation:
    pred = _norm(predicate_surface or predicate).replace(" ", "_")
    canonical = _norm(predicate).replace(" ", "_")
    object_text = _norm(object_value)
    object_is_internal_frame = object_frame_id is not None or object_text.startswith("frame:")
    standalone = _standalone(
        frame_role=frame_role,
        subject=subject,
        predicate=pred or canonical,
        resolution_status=resolution_status,
    )

    cues: list[str] = []
    semantic_type = "UNKNOWN"
    confidence = 0.45
    structural = {
        "located_at": "LOCATION", "be_in": "LOCATION", "be_at": "LOCATION",
        "identity": "IDENTITY", "same_as": "IDENTITY", "type_of": "IDENTITY",
        "member_of": "RELATION", "friend_of": "RELATION", "enemy_of": "RELATION",
        "ally_of": "RELATION", "parent_of": "RELATION", "spouse_of": "RELATION",
        "works_for": "RELATION", "employed_by": "RELATION", "must": "RULE",
        "must_not": "RULE", "required": "RULE", "forbidden": "RULE",
        "allowed": "RULE", "before": "TEMPORAL", "after": "TEMPORAL", "during": "TEMPORAL",
    }
    if canonical in structural:
        semantic_type = structural[canonical]
        confidence = 0.94
        cues.append(f"canonical:{canonical}")
    elif pred in _ENGLISH_LEMMA_HINTS:
        semantic_type = _ENGLISH_LEMMA_HINTS[pred]
        confidence = 0.90
        cues.append(f"{source_language}_lemma:{pred}")
    elif canonical in _ENGLISH_LEMMA_HINTS:
        semantic_type = _ENGLISH_LEMMA_HINTS[canonical]
        confidence = 0.88
        cues.append(f"{source_language}_canonical:{canonical}")
    elif pred == "be" or canonical in {"be", "be_definition_of"}:
        words = {token.strip(".,:;!?()[]{}\"'") for token in object_text.split() if token}
        first = next(iter(object_text.split()), "")
        if first in _LOCATION_PREPOSITIONS:
            semantic_type, confidence = "LOCATION", 0.86
            cues.append("copula:locative")
        elif words & _IDENTITY_NOUNS:
            semantic_type, confidence = "IDENTITY", 0.82
            cues.append("copula:class_or_role")
        else:
            semantic_type, confidence = "DESCRIPTION", 0.80
            cues.append("copula:predicative")
    elif pred:
        semantic_type, confidence = "ACTION", 0.66
        cues.append("open_class_predicate")

    if object_is_internal_frame:
        cues.append("nested_content")
    role = _norm(frame_role)
    if role in _DEPENDENT_ROLES:
        cues.append(f"dependent_clause:{role}")
    if meta and meta.get("passive_reporting"):
        cues.append("passive_reporting")

    return SemanticInterpretation(
        semantic_type=semantic_type,
        confidence=confidence,
        standalone_semantic=standalone,
        source_language=source_language,
        cues=tuple(cues),
    )


def family_from_semantic_type(semantic_type: Optional[str]) -> Optional[str]:
    return SEMANTIC_TYPE_TO_PREDICATE_FAMILY.get((semantic_type or "").upper())


def claim_kind_from_semantic_type(semantic_type: Optional[str]) -> Optional[str]:
    return SEMANTIC_TYPE_TO_CLAIM_KIND.get((semantic_type or "").upper())
