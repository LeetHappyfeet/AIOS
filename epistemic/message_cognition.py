from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService, valid_goal_objective

INTERPRETER_VERSION = "message-cognition-v9-candidate-admission"
MAX_UNITS = 12

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"[a-z0-9_'-]+", re.I)
_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|no\s+longer|cannot|can't|isn't|aren't|wasn't|weren't|hasn't|haven't|hadn't|don't|doesn't|didn't|won't)\b",
    re.I,
)
_QUESTION_RE = re.compile(r"\?\s*$")
_CAUSAL_DESIRE_RE = re.compile(
    r"\b(?:made|makes|making|caused|causes|causing)\s+(?:me|you|him|her|them|us|[a-z0-9_-]+)\s+want\b",
    re.I,
)

_SUBJECT = r"(?P<subject>I|you|she|he|they|we|it|[A-Za-z][A-Za-z0-9_-]{1,48})"
_COMMITMENT_RE = re.compile(
    r"\b(?P<subject>I)(?:\s+(?P<verb>will|am\s+going\s+to)|(?P<shortverb>'ll|'m\s+going\s+to))\s+"
    r"(?P<object>[^.!?]{3,220})", re.I,
)
_REFUSAL_RE = re.compile(
    r"\bI(?:\s+am\s+not|'m\s+not)\s+going\s+anywhere\b|"
    r"\bI(?:\s+will\s+not|\s+won't|\s+refuse\s+to|"
    r"\s+am\s+not\s+going\s+to|'m\s+not\s+going\s+to)"
    r"\s+(?P<refused>[^.!?]{3,160})", re.I,
)
_DISCOURSE_MARKER_RE = re.compile(r"\byou\s+know\s+what\s*[,—:]", re.I)
_GOAL_RE = re.compile(
    rf"\b{_SUBJECT}\s+(?:(?:do(?:es)?\s+not|don't|doesn't|no\s+longer)\s+)?"
    rf"(?P<verb>want(?:s|ed)?|intend(?:s|ed)?|plan(?:s|ned)?|need(?:s|ed)?|"
    rf"seek(?:s|ed)?|decide(?:s|d)?|resolve(?:s|d)?|prepare(?:s|d)?)"
    r"\s+(?P<object>(?:to\s+)?[^.!?]{2,220})",
    re.I,
)
_MEMORY_RE = re.compile(
    rf"\b{_SUBJECT}\s+(?P<verb>remember(?:s|ed)?|recall(?:s|ed)?|recognize(?:s|d)?|forgot|forgets|forget)"
    r"\s+(?P<object>[^.!?]{2,220})",
    re.I,
)
_BELIEF_RE = re.compile(
    rf"\b{_SUBJECT}\s+(?P<verb>believe(?:s|d)?|think(?:s)?|thought|know(?:s)?|knew|suspect(?:s|ed)?|"
    rf"assume(?:s|d)?|wonder(?:s|ed)?)\s+(?P<object>[^.!?]{{2,220}})",
    re.I,
)
_UNCERTAIN_RE = re.compile(
    rf"\b{_SUBJECT}\s+(?P<verb>is|was|seems|seemed)\s+(?P<object>uncertain|unsure|confused)\b(?P<tail>[^.!?]{{0,180}})",
    re.I,
)
_RULE_RE = re.compile(
    rf"\b{_SUBJECT}\s+(?P<verb>must|mustn't|cannot|can't|may\s+not|is\s+required\s+to|"
    rf"is\s+allowed\s+to|is\s+forbidden\s+to)\s+(?P<object>[^.!?]{{2,220}})",
    re.I,
)
_RELATIONSHIP_RE = re.compile(
    r"\b(?:friend|enemy|ally|partner|assistant|sidekick|battle buddy|trust(?:s|ed|ing)?|"
    r"distrust(?:s|ed|ing)?|offer(?:s|ed|ing)?\s+to\s+help|help(?:s|ed|ing)?\s+(?:me|you|her|him|them)|"
    r"work(?:s|ed|ing)?\s+(?:with|for))\b",
    re.I,
)
_RELATIONSHIP_SUBJECT_RE = re.compile(
    r"(?:^|[\s\"'“‘(])(?P<subject>I|you|she|he|they|we|it|[A-Za-z][A-Za-z0-9_-]{1,48})"
    r"(?=\s+(?:am|is|are|was|were|be|being|become|became|remain|remains|trust|trusts|distrust|distrusts|work|works|offer|offers|offered|help|helps|helped)\b|"
    r"(?:['’](?:m|re|s|ve|d|ll))\b)",
    re.I,
)
_STATE_TERMS_RE = re.compile(
    r"\b(?:alive|dead|digital|data|computer|body|form|real|physical|trapped|free|inside|outside|within|"
    r"located|location|exists?|existing|conscious|awake|asleep|injured|armed|powered|human|artificial)\b",
    re.I,
)
_STATE_RE = re.compile(
    rf"\b{_SUBJECT}\s+(?P<verb>isn't|aren't|wasn't|weren't|hasn't|haven't|hadn't|is|am|are|was|were|has|have|had|exists?|became|becomes|remains?)\s+"
    r"(?P<object>[^.!?]{2,220})",
    re.I,
)
_EVENT_RE = re.compile(
    r"\b(?:tried|attempted|arrived|left|moved|entered|escaped|attacked|fought|gave|took|opened|closed|activated|"
    r"deactivated|created|destroyed|transferred|rescued|captured|released|changed|returned|appeared|vanished)\b",
    re.I,
)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "hers", "him", "his", "i", "in", "is", "it",
    "its", "me", "my", "of", "on", "or", "our", "she", "that", "the", "their", "them",
    "they", "this", "to", "was", "we", "were", "with", "you", "your",
}

_PERSISTENCE = {
    "MEMORY": "durable", "BELIEF": "until_contradicted", "GOAL": "session",
    "RULE": "until_contradicted", "RELATIONSHIP": "until_contradicted",
    "STATE": "until_changed", "EVENT": "turn",
}
_KIND_BASE_SALIENCE = {
    "MEMORY": 0.78, "BELIEF": 0.72, "GOAL": 0.82, "RULE": 0.74,
    "RELATIONSHIP": 0.70, "STATE": 0.68, "EVENT": 0.48,
}
_NEGATED_VERBS = {
    "isn't": "is not", "aren't": "are not", "wasn't": "was not", "weren't": "were not",
    "hasn't": "does not have", "haven't": "do not have", "hadn't": "did not have",
}

@dataclass(frozen=True)
class CognitiveUnit:
    text: str
    claim_kind: str
    topic_key: str
    polarity: int
    salience: float
    confidence: float
    meta: dict

@dataclass(frozen=True)
class QuestionSemantics:
    """Transient semantics for retrieval routing; never persisted as cognition."""

    subject: str
    relation: str
    topic_terms: tuple[str, ...]
    temporal_scope: str
    confidence: float
    source_text: str


_QUESTION_STOPWORDS = _STOPWORDS | {
    "what", "which", "who", "where", "when", "why", "how", "much", "many",
    "do", "does", "did", "would", "could", "should", "can", "ever", "actually",
    "before", "today", "about", "really", "something", "anything",
}
_QUESTION_RELATIONS = (
    ("preference", re.compile(
        r"\b(?:like|likes|liked|prefer|prefers|preferred|preference|favorite|favourite|"
        r"want|wants|wanted|care|cares|cared|interest|interests|interested|appeal|"
        r"choose|chooses|chose|pick|picks|picked)\b", re.I
    )),
    ("memory", re.compile(r"\b(?:remember|remembers|recall|recalls|memory)\b", re.I)),
    ("experience", re.compile(
        r"\b(?:visit|visited|meet|met|go|went|been|see|saw|read|buy|bought|try|tried|"
        r"experience|experienced|wear|wore|worn|use|used|own|owned|have|had)\b", re.I
    )),
)
_QUESTION_SECOND_PERSON_RE = re.compile(r"\b(?:you|your|yours|yourself)\b", re.I)


def question_semantics(
    text: str,
    *,
    character_id: str,
    speaker_id: str | None,
) -> QuestionSemantics | None:
    """Extract high-confidence character-directed question semantics.

    This complements interpret_message: questions remain non-assertions and
    are never persisted, but their subject/relation/topic can guide retrieval.
    """
    questions = [sentence for sentence in _sentences(text) if _QUESTION_RE.search(sentence)]
    if not questions:
        return None
    sentence = questions[-1]
    if not _QUESTION_SECOND_PERSON_RE.search(sentence):
        return None
    if speaker_id and _same_identity(speaker_id, character_id):
        return None

    relation = ""
    relation_words: set[str] = set()
    hypothetical_wear = re.search(
        r"\b(?:would|could|might)\b[^?]{0,100}\b(?:wear|wearing|choose|pick)\b",
        sentence,
        re.I,
    )
    if hypothetical_wear:
        relation = "preference"
        relation_words.update(
            token.lower()
            for token in _WORD_RE.findall(hypothetical_wear.group(0))
            if token.lower() in {"wear", "wearing", "choose", "pick"}
        )
    for candidate_relation, pattern in _QUESTION_RELATIONS:
        if relation:
            break
        matches = pattern.findall(sentence)
        if matches:
            relation = candidate_relation
            relation_words.update(str(value).lower() for value in matches)
            break
    if not relation:
        return None

    tokens = [
        token.lower() for token in _WORD_RE.findall(sentence)
        if len(token) >= 2 and token.lower() not in _QUESTION_STOPWORDS
    ]
    topic_terms = tuple(token for token in tokens if token not in relation_words)[:8]
    if not topic_terms:
        return None
    temporal_scope = (
        "prior"
        if re.search(r"\b(?:before|ever|previously|used to|in the past)\b", sentence, re.I)
        else "unspecified"
    )
    return QuestionSemantics(
        subject=character_id,
        relation=relation,
        topic_terms=topic_terms,
        temporal_scope=temporal_scope,
        confidence=0.90,
        source_text=sentence[:500],
    )


@dataclass(frozen=True)
class ParsedCandidate:
    kind: str
    subject_text: str | None
    predicate: str
    object_text: str
    confidence: float
    reason: str


def _runtime_versions() -> dict[str, str]:
    from aios_app.epistemic.runtime_versions import component_versions
    return component_versions()


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.split(text or "") if part.strip()]


def _identity_aliases(value: str | None) -> set[str]:
    if not value:
        return set()
    clean = re.sub(r"[^a-z0-9_-]+", " ", value.lower()).strip()
    aliases = {clean} if clean else set()
    for token in re.split(r"[_\-\s]+", clean):
        if len(token) >= 2 and not token.isdigit():
            aliases.add(token)
    stem = re.sub(r"[_-]?\d+$", "", clean).strip("_- ")
    if stem:
        aliases.add(stem)
    return aliases


def _same_identity(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return bool(_identity_aliases(left) & _identity_aliases(right))


def _resolve_subject(subject: str | None, *, character_id: str, speaker_id: str | None, viewpoint_id: str | None) -> tuple[str | None, bool]:
    if not subject:
        return None, False
    raw = subject.strip().lower()
    character_aliases = _identity_aliases(character_id)
    speaker_aliases = _identity_aliases(speaker_id)
    viewpoint_aliases = _identity_aliases(viewpoint_id)
    if raw in {"i", "we"}:
        owner = speaker_id or viewpoint_id
        return owner, _same_identity(owner, character_id)
    if raw == "you":
        # An external speaker addressing a character is not authoring that character's
        # beliefs, memories, rules, or goals. Preserve attributed source text.
        if speaker_id and not _same_identity(speaker_id, character_id):
            return character_id, False
        return "other", False
    if raw in {"she", "he", "they", "it"}:
        if viewpoint_aliases & character_aliases:
            return character_id, True
        return None, False
    if raw in character_aliases:
        return character_id, True
    if raw in speaker_aliases:
        return speaker_id, _same_identity(speaker_id, character_id)
    return subject, False


def _canonical_subject(owner: str | None, fallback: str | None) -> str:
    value = owner or fallback or "someone"
    return re.sub(r"[_-]\d+$", "", str(value)).replace("_", " ").strip()


def _clean_object(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip(" \t\n\r,;:-"))
    return text[:220].rstrip()


def _bounded_goal_candidate(candidate: ParsedCandidate) -> ParsedCandidate:
    """Do not combine a negated desire with a subsequent independent clause."""
    objective = re.split(r"[,;]\s*(?:but|and)\b", candidate.object_text,
                         maxsplit=1, flags=re.I)[0].strip()
    return ParsedCandidate(candidate.kind, candidate.subject_text, candidate.predicate,
                           objective, candidate.confidence, candidate.reason)


def _goal_polarity(sentence: str, candidate: ParsedCandidate) -> int:
    # Inspect the goal predicate, not negation in an unrelated following clause.
    end = sentence.lower().find(candidate.object_text.lower()) if candidate.object_text else -1
    lead = sentence[:end] if end >= 0 else sentence
    return -1 if re.search(
        r"\b(?:don't|doesn't|do not|does not|never|no longer)\s+"
        r"(?:need|want|plan|intend|seek|decide|prepare)\b", lead, re.I
    ) else 1


def _goal_semantics(candidate: ParsedCandidate) -> tuple[str, str]:
    predicate = candidate.predicate.lower().strip()
    if predicate.startswith(("want", "need", "seek")):
        return "desire", "session"
    if predicate.startswith(("plan", "prepare", "intend")):
        return "plan", "session"
    if predicate.startswith("commit"):
        return "commitment", "session"
    if predicate.startswith(("decide", "resolve")):
        return "objective", "session"
    return "objective", "session"


def _canonical_text(candidate: ParsedCandidate, *, owner: str | None) -> str:
    subject = _canonical_subject(owner, candidate.subject_text)
    obj = _clean_object(candidate.object_text)
    predicate = _NEGATED_VERBS.get(candidate.predicate.lower().strip(), candidate.predicate.lower().strip())
    if candidate.kind == "MEMORY":
        return f"{subject} remembers {obj}."
    if candidate.kind == "BELIEF":
        if predicate in {"is", "was", "seems", "seemed"} and obj.startswith(("uncertain", "unsure", "confused")):
            return f"{subject} is {obj}."
        return f"{subject} {predicate} {obj}."
    if candidate.kind == "GOAL":
        # Goal text is a presentation of semantic intent, not a grammatical
        # rewrite of the source sentence. This avoids rendering an uninflected predicate for the character name.
        intent_type, _ = _goal_semantics(candidate)
        verb = "wants" if intent_type == "desire" else "intends"
        if intent_type == "commitment" and not obj.casefold().startswith("to "):
            obj = "to " + obj
        return f"{subject} {verb} {obj}."
    if candidate.kind in {"RULE", "STATE"}:
        return f"{subject} {predicate} {obj}."
    return f"{subject}: {obj}." if candidate.subject_text else f"{obj}."


def _parse_sentence(sentence: str) -> ParsedCandidate | None:
    sentence = sentence.replace('’', "'")
    # Discourse marker is not a knowledge assertion by the addressee.
    if _DISCOURSE_MARKER_RE.search(sentence):
        return None
    if _QUESTION_RE.search(sentence):
        return None
    refused = _REFUSAL_RE.search(sentence)
    if refused:
        action = _clean_object(refused.group("refused") or "leave")
        action = re.sub(r"(?i)^to\s+", "", action)
        return ParsedCandidate("STATE", "I", "refuses", "to " + action,
                               0.89, "expressed_scene_refusal")
    match = _MEMORY_RE.search(sentence)
    if match:
        return ParsedCandidate("MEMORY", match.group("subject"), match.group("verb"), match.group("object"), 0.94, "memory_predicate")
    match = _BELIEF_RE.search(sentence)
    if match:
        return ParsedCandidate("BELIEF", match.group("subject"), match.group("verb"), match.group("object"), 0.92, "belief_predicate")
    match = _UNCERTAIN_RE.search(sentence)
    if match:
        obj = f"{match.group('object')}{match.group('tail') or ''}"
        return ParsedCandidate("BELIEF", match.group("subject"), match.group("verb"), obj, 0.90, "uncertainty_state")
    match = _RULE_RE.search(sentence)
    if match:
        return ParsedCandidate("RULE", match.group("subject"), match.group("verb"), match.group("object"), 0.93, "deontic_predicate")
    if not _CAUSAL_DESIRE_RE.search(sentence):
        match = _GOAL_RE.search(sentence)
        if match:
            bounded = _bounded_goal_candidate(ParsedCandidate(
                "GOAL", match.group("subject"), match.group("verb"),
                match.group("object"), 0.91, "goal_predicate"))
            if valid_goal_objective(bounded.object_text):
                return bounded
    # Recognize a self-authored future action but do not interpret an
    # auxiliary such as "going to need" as a commitment to perform "go".
    if not re.search(r"\b(?:if|unless|provided\s+that)\b", sentence, re.I):
        match = _COMMITMENT_RE.search(sentence)
        if match:
            action = _bounded_goal_candidate(ParsedCandidate(
                "GOAL", match.group("subject"), "commit",
                match.group("object"), 0.86, "explicit_self_commitment"))
            words = re.findall(r"[A-Za-z0-9]+", action.object_text)
            if (valid_goal_objective(action.object_text)
                    and len(words) >= 2
                    and not re.search(r"(?i)\b(?:how|something|whatever)$", action.object_text)
                    and not re.match(r"(?i)^(?:do|take|handle|make) it$", action.object_text)
                    and not re.match(r"(?i)^need\s+(?:to\s+)?", action.object_text)):
                return action
    if _RELATIONSHIP_RE.search(sentence):
        subject_match = _RELATIONSHIP_SUBJECT_RE.search(sentence)
        subject = subject_match.group("subject") if subject_match else None
        return ParsedCandidate("RELATIONSHIP", subject, "relates", sentence, 0.82, "relationship_predicate")
    match = _STATE_RE.search(sentence)
    if match and _STATE_TERMS_RE.search(match.group("object")):
        return ParsedCandidate("STATE", match.group("subject"), match.group("verb"), match.group("object"), 0.86, "bounded_state_predicate")
    if _EVENT_RE.search(sentence):
        # Explicit leading pronouns have a grammatical actor. Other narrative
        # openings (prepositions, description, location) remain unresolved.
        lead = re.match(
            r"^\s*[\"'*]*(?P<actor>I|he|she|they|we)\s+"
            r"(?:tried|attempted|arrived|left|moved|entered|escaped|attacked|"
            r"fought|gave|took|opened|closed|activated|deactivated|created|"
            r"destroyed|transferred|rescued|captured|released|changed|"
            r"returned|appeared|vanished)\b", sentence, re.I,
        )
        subject = lead.group("actor") if lead else None
        return ParsedCandidate("EVENT", subject, "event", sentence, 0.70, "event_predicate")
    return None


def cognition_topic_key(
    text: str, *, character_id: str, owner: str | None, kind: str,
    objective: str | None = None,
) -> str:
    semantic_text = objective if kind.upper() == "GOAL" and objective else text
    tokens = [token for token in _WORD_RE.findall(semantic_text.lower()) if len(token) >= 3 and token not in _STOPWORDS and token not in {"not", "never", "cannot", "can't", "want", "wants", "wanted", "intend", "intends", "intended"}]
    preferred: list[str] = [kind.lower()]
    for value in (owner, character_id):
        for token in _identity_aliases(value):
            if token and token not in preferred:
                preferred.append(token)
                break
    for token in tokens:
        if token not in preferred:
            preferred.append(token)
        if len(preferred) >= 6:
            break
    return ":".join(preferred[:6]) or f"{kind.lower()}:message"


def _score_candidate(candidate: ParsedCandidate, *, character_owned: bool, index: int, total: int) -> float:
    score = _KIND_BASE_SALIENCE[candidate.kind]
    if character_owned:
        score += 0.10
    if index >= max(0, total - 2):
        score += 0.03
    return max(0.0, min(1.0, score))


def interpret_message(text: str, *, character_id: str, speaker_id: str | None, speaker_role: str | None, viewpoint_id: str | None, diagnostics: list[dict] | None = None) -> list[CognitiveUnit]:
    sentences = _sentences(text)
    if not sentences:
        return []
    perspective_id = viewpoint_id or speaker_id
    ranked: list[tuple[float, int, CognitiveUnit]] = []
    seen_sources: set[str] = set()
    seen_semantics: set[tuple[str, str, int]] = set()
    def reject(index: int, reason: str, source: str) -> None:
        if diagnostics is not None and len(diagnostics) < 32:
            diagnostics.append({"sentence_index": index, "reason": reason,
                                "source_excerpt": source[:240]})

    for index, sentence in enumerate(sentences):
        normalized_source = " ".join(_WORD_RE.findall(sentence.lower()))
        if not normalized_source or normalized_source in seen_sources:
            reject(index, "empty_or_duplicate", sentence)
            continue
        seen_sources.add(normalized_source)
        candidate = _parse_sentence(sentence)
        if candidate is None:
            reason = ("question" if _QUESTION_RE.search(sentence) else
                      "discourse_marker" if _DISCOURSE_MARKER_RE.search(sentence) else
                      "ambiguous_intention_or_agreement" if re.search(
                          r"(?i)\b(?:deal|agreed|accept|rather|going to need)\b",
                          sentence) else "no_supported_candidate")
            reject(index, reason, sentence)
            continue
        # An external request addressed to the character is not an adopted goal.
        # The fast-path goal writer requires self-authored intention evidence.
        if candidate.kind == "GOAL" and (
            str(candidate.subject_text or "").casefold() == "you"
            or not _same_identity(speaker_id, character_id)
        ):
            reject(index, "external_goal_not_adopted", sentence)
            continue
        owner, character_owned = _resolve_subject(candidate.subject_text, character_id=character_id, speaker_id=speaker_id, viewpoint_id=viewpoint_id)
        if not _same_identity(speaker_id, character_id) and candidate.kind in {
            "MEMORY", "BELIEF", "RULE", "STATE", "RELATIONSHIP",
        }:
            # Externally attributed testimony is not self-authored subjective
            # character state; retain observable claims with unowned provenance.
            character_owned = False
        if candidate.kind in {"MEMORY", "BELIEF", "GOAL", "RULE"} and not character_owned:
            reject(index, "character_ownership_unresolved", sentence)
            continue
        polarity = (
            1 if candidate.reason == "expressed_scene_refusal"
            else _goal_polarity(sentence, candidate) if candidate.kind == "GOAL"
            else (-1 if _NEGATION_RE.search(sentence) else 1)
        )
        canonical = _canonical_text(candidate, owner=owner)
        objective = _clean_object(candidate.object_text) if candidate.kind == "GOAL" else None
        topic_key = cognition_topic_key(
            canonical, character_id=character_id, owner=owner, kind=candidate.kind,
            objective=objective,
        )
        semantic_key = (candidate.kind, topic_key, polarity)
        if semantic_key in seen_semantics:
            reject(index, "duplicate_semantic_candidate", sentence)
            continue
        seen_semantics.add(semantic_key)
        salience = _score_candidate(candidate, character_owned=character_owned, index=index, total=len(sentences))
        confidence = max(0.50, min(0.99, candidate.confidence))
        goal_meta = {}
        if candidate.kind == "GOAL":
            intent_type, horizon = _goal_semantics(candidate)
            goal_meta = {
                "intent_type": intent_type,
                "horizon": horizon,
                "objective": objective,
            }
        unit = CognitiveUnit(
            text=canonical, claim_kind=candidate.kind, topic_key=topic_key, polarity=polarity,
            salience=salience, confidence=confidence,
            meta={
                "message_scope": True, "speaker_id": speaker_id, "speaker_role": speaker_role,
                "viewpoint_id": viewpoint_id, "perspective_id": perspective_id,
                "semantic_owner": owner, "character_owned": character_owned,
                "sentence_index": index, "source_text": sentence[:500],
                "predicate": candidate.predicate.lower(), "object": _clean_object(candidate.object_text),
                "parse_reason": candidate.reason,
                "scene_position": (
                    "refusal" if candidate.reason == "expressed_scene_refusal" else None
                ),
                "persistence": _PERSISTENCE[candidate.kind],
                "parse_confidence": confidence, "epistemic_confidence": 0.72 if character_owned else 0.58,
                **goal_meta,
            },
        )
        ranked.append((salience, index, unit))
    ranked.sort(key=lambda value: (-value[0], value[1]))
    selected = ranked[:MAX_UNITS]
    if len(ranked) > MAX_UNITS:
        for _, index, unit in ranked[MAX_UNITS:]:
            reject(index, "unit_budget_exceeded", str(unit.meta.get("source_text") or ""))
    selected.sort(key=lambda value: value[1])
    return [unit for _, _, unit in selected]


def ambiguous_cognition_sentences(
    text: str, *, character_id: str, speaker_id: str | None,
    speaker_role: str | None, viewpoint_id: str | None,
) -> list[str]:
    """Return a tiny bounded set worth semantic adjudication by an inference worker.

    Explicit deterministic units remain authoritative fast-path results. The
    classifier only sees declarative character-authored prose the cheap parser
    could not type, preventing one LLM call from becoming a second parser for
    the entire message.
    """
    if not _same_identity(speaker_id, character_id):
        return []
    explicit_units = interpret_message(
        text,character_id=character_id,speaker_id=speaker_id,
        speaker_role=speaker_role,viewpoint_id=viewpoint_id)
    explicit_sources={
        str(unit.meta.get("source_text") or "").strip()
        for unit in explicit_units
    }
    # Explicit fast-path goals remain authoritative, but future timing still
    # benefits from one bounded temporal review. Reuse the original unit when
    # the enrichment worker admits that proposal; never classify all fast-path
    # cognition again.
    explicit_timer_goals={
        str(unit.meta.get("source_text") or "").strip()
        for unit in explicit_units
        if unit.claim_kind == "GOAL" and unit.polarity > 0
        and bool(unit.meta.get("character_owned"))
        and re.search(
            r"\b(?:today|tomorrow|tonight|later|soon|afterwards|sometime later|"
            r"in\s+\d+\s+(?:minutes?|hours?|days?|weeks?))\b",
            str(unit.meta.get("source_text") or ""), re.I,
        )
    }
    candidates=[]
    message_sentences = _sentences(text)
    for sentence_index, sentence in enumerate(message_sentences):
        clean=sentence.strip()
        explicit_timer_goal = clean in explicit_timer_goals
        if (not clean or (clean in explicit_sources and not explicit_timer_goal)
                or _QUESTION_RE.search(clean)):
            continue
        # Dialogue/action prose with first-person commitment, future intent,
        # offers/agreements, or self-development language is high-value enough
        # to adjudicate. This is candidate generation, never goal authority.
        lower=clean.lower().replace("’", "'")
        signals=(
            "i'll ","i will ","i'm going to ","i am going to ","i should ",
            "i could ","my goal","my plan","counter-offer","standing offer",
            "deal.","deal,","agreed.","agreed,","i accept","i'm going to need",
            "i'm learning","i am learning","i'd rather","i would rather",
        )
        if explicit_timer_goal or any(signal in lower for signal in signals):
            # Keep the immediately preceding sentence: commitments are often
            # elliptical ("Tomorrow afternoon. I'll go.") and the date is
            # essential to distinguishing a future plan from an immediate act.
            excerpt = clean
            if sentence_index > 0:
                previous = message_sentences[sentence_index - 1].strip()
                if previous:
                    excerpt = f"{previous} {clean}"
            candidates.append(excerpt[:700])
        if len(candidates)>=4:
            break
    return candidates


async def _reconcile_unit(db: Any, *, instance_id: UUID, unit_id: UUID, claim_kind: str, topic_key: str, polarity: int) -> UUID | None:
    if claim_kind not in {"BELIEF", "STATE", "GOAL", "RELATIONSHIP", "RULE"}:
        return None
    previous = await db.fetchrow(
        """
        SELECT u.unit_id, u.polarity
        FROM aios.message_cognitive_unit u
        JOIN aios.message_cognitive_commit c ON c.commit_id=u.commit_id
        WHERE c.instance_id=$1 AND u.unit_id<>$2 AND u.claim_kind=$3 AND u.topic_key=$4 AND u.status='active'
        ORDER BY c.event_id DESC NULLS LAST, u.created_at DESC
        LIMIT 1
        """,
        instance_id, unit_id, claim_kind, topic_key,
    )
    if not previous or int(previous["polarity"] or 1) == polarity:
        return None
    previous_id = previous["unit_id"]
    await db.execute("UPDATE aios.message_cognitive_unit SET status='superseded' WHERE unit_id=$1", previous_id)
    await db.execute(
        """UPDATE aios.message_cognitive_unit
        SET supersedes_unit_id=$2, meta=meta || jsonb_build_object('reconciled_polarity_flip', true)
        WHERE unit_id=$1""",
        unit_id, previous_id,
    )
    return previous_id


async def commit_message_cognition(
    db: Database, *, instance_id: UUID, node_id: UUID,
    expected_head_node_id: UUID | None = None,
) -> bool:
    # The same live node can be scheduled concurrently by activation/HUD work.
    # Serialize the full rebuild so DELETE + ordinal INSERT is one atomic owner.
    lock_key = f"message-cognition::{instance_id}::{node_id}"
    async with db.connection() as con:
        async with con.transaction():
            await con.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                lock_key,
            )
            committed = await _commit_message_cognition_locked(
                con, instance_id=instance_id, node_id=node_id,
                expected_head_node_id=expected_head_node_id,
            )
    if committed:
        row = await db.fetchrow(
            """SELECT summary FROM aios.message_cognitive_commit
               WHERE instance_id=$1 AND node_id=$2""",instance_id,node_id)
        summary = CharacterGoalService._json_object(row["summary"]) if row else {}
        if "GOAL" in summary.get("kinds", []):
            # Replaying an old source node can recover an intention without
            # writing a historical scene over the current HUD head.
            live = await db.fetchval(
                "SELECT 1 FROM aios.character_hud_readiness "
                "WHERE instance_id=$1 AND source_head_node_id=$2",
                instance_id, node_id,
            )
            if live:
                from aios_app.epistemic.scene_resolver import CharacterSceneProjector
                await CharacterSceneProjector(db).refresh(
                    instance_id, source_head_node_id=node_id
                )
        # Historical catch-up only persists candidate evidence. Its goal and
        # enrichment consequences require the chronological reconciliation
        # stage, rather than mutating current character state out of order.
        if (not summary.get("historical_catchup")
                and summary.get("ambiguous_sentences")
                and summary.get("enrichment_pending")):
            from aios_app.pipeline.jobs import enqueue_job
            await enqueue_job(
                db,job_type="message_cognition_enrichment",
                payload={"instance_id":str(instance_id),"node_id":str(node_id)},
                priority=35)
    return committed


async def _commit_message_cognition_locked(
    con: Any, *, instance_id: UUID, node_id: UUID,
    expected_head_node_id: UUID | None = None,
) -> bool:
    # Re-read only after acquiring the lock. A competing worker may have
    # completed this exact cognition commit while we were waiting.
    row = await con.fetchrow(
        """
        SELECT dn.node_id, dn.timeline_id, dn.event_id, dn.message_text,
               dn.speaker_id, dn.speaker_role::text AS speaker_role,
               COALESCE(NULLIF(dn.viewpoint_id,''), NULLIF(dn.payload->>'viewpoint_id','')) AS viewpoint_id,
               ci.character_id
        FROM aios.dag_node dn
        JOIN aios.character_instance ci ON ci.instance_id=$1
        JOIN aios.character_runtime_state rs ON rs.instance_id=ci.instance_id
        JOIN aios.dag_node live_head
          ON live_head.node_id=rs.source_head_node_id
         AND live_head.timeline_id=rs.source_timeline_id
        WHERE dn.node_id=$2
          AND dn.timeline_id=rs.source_timeline_id
          AND dn.event_id<=live_head.event_id
          AND ($3::uuid IS NULL OR rs.source_head_node_id=$3)
        FOR SHARE OF rs
        """,
        instance_id, node_id, expected_head_node_id,
    )
    if not row:
        return False
    text = str(row["message_text"] or "").strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    live_head = await con.fetchval(
        "SELECT source_head_node_id=$2 FROM aios.character_runtime_state WHERE instance_id=$1",
        instance_id, node_id,
    )
    existing = await con.fetchrow(
        "SELECT commit_id, source_text_hash, interpreter_version FROM aios.message_cognitive_commit WHERE instance_id=$1 AND node_id=$2",
        instance_id, node_id,
    )
    if existing and existing["source_text_hash"] == digest and existing["interpreter_version"] == INTERPRETER_VERSION:
        await _advance_cognitive_cursor_on_connection(con, instance_id=instance_id, node_id=node_id, event_id=row["event_id"])
        return True
    candidate_diagnostics: list[dict] = []
    units = interpret_message(
        text, character_id=str(row["character_id"]), speaker_id=row["speaker_id"],
        speaker_role=row["speaker_role"], viewpoint_id=row["viewpoint_id"],
        diagnostics=candidate_diagnostics,
    )
    ambiguous = ambiguous_cognition_sentences(
        text, character_id=str(row["character_id"]), speaker_id=row["speaker_id"],
        speaker_role=row["speaker_role"], viewpoint_id=row["viewpoint_id"],
    )
    summary = {
        "unit_count": len(units), "kinds": sorted({unit.claim_kind for unit in units}),
        "candidate_audit_version": "cognition-candidate-audit-v1",
        "candidate_outcome": (
            "explicit_units" if units else
            "bounded_enrichment_pending" if ambiguous else
            "zero_units_explained"
        ),
        "candidate_rejections": candidate_diagnostics,
        "candidate_rejection_count": len(candidate_diagnostics),
        "candidate_audit_complete": len(candidate_diagnostics) < 32,
        "runtime_versions": _runtime_versions(),
        "participants": [value for value in (row["speaker_id"], row["character_id"]) if value],
        "bounded": True, "max_units": MAX_UNITS, "interpreter_version": INTERPRETER_VERSION,
        "historical_catchup": not bool(live_head),
        "enrichment_deferred": bool(not live_head and ambiguous),
        "polarity_reconciliation_deferred": not bool(live_head),
        "goal_projection_deferred": bool(not live_head and any(
            u.claim_kind == "GOAL" and u.meta.get("character_owned") for u in units
        )),
        "ambiguous_count": len(ambiguous), "enrichment_pending": bool(ambiguous),
    }
    commit_row = await con.fetchrow(
        """
        INSERT INTO aios.message_cognitive_commit (
            instance_id, node_id, timeline_id, event_id, character_id,
            speaker_id, speaker_role, viewpoint_id, interpreter_version,
            source_text_hash, summary, committed_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,now())
        ON CONFLICT (instance_id, node_id) DO UPDATE
        SET timeline_id=EXCLUDED.timeline_id, event_id=EXCLUDED.event_id,
            character_id=EXCLUDED.character_id, speaker_id=EXCLUDED.speaker_id,
            speaker_role=EXCLUDED.speaker_role, viewpoint_id=EXCLUDED.viewpoint_id,
            interpreter_version=EXCLUDED.interpreter_version,
            source_text_hash=EXCLUDED.source_text_hash, summary=EXCLUDED.summary,
            committed_at=now(), enrichment_completed_at=NULL
        RETURNING commit_id
        """,
        instance_id, node_id, row["timeline_id"], row["event_id"], row["character_id"],
        row["speaker_id"], row["speaker_role"], row["viewpoint_id"], INTERPRETER_VERSION,
        digest, json.dumps(summary),
    )
    commit_id = commit_row["commit_id"]
    await con.execute("DELETE FROM aios.message_cognitive_unit WHERE commit_id=$1", commit_id)
    for ordinal, unit in enumerate(units):
        unit_row = await con.fetchrow(
            """
            INSERT INTO aios.message_cognitive_unit (
                commit_id, ordinal, claim_kind, text, topic_key, polarity,
                salience, confidence, status, meta
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active',$9::jsonb)
            RETURNING unit_id
            """,
            commit_id, ordinal, unit.claim_kind, unit.text, unit.topic_key, unit.polarity,
            unit.salience, unit.confidence, json.dumps(unit.meta),
        )
        if live_head:
            await _reconcile_unit(
                con, instance_id=instance_id, unit_id=unit_row["unit_id"],
                claim_kind=unit.claim_kind, topic_key=unit.topic_key,
                polarity=unit.polarity,
            )
        # Historical units remain active as source evidence, but cannot
        # supersede newer live cognitive units out of temporal order.
        if (live_head and unit.claim_kind == "GOAL"
                and bool(unit.meta.get("character_owned"))):
            # Older recovered evidence is persisted as a unit but cannot
            # retroactively override a newer goal. A separate chronological
            # goal lifecycle replay will adjudicate it in the next patch.
            await CharacterGoalService(con).reconcile_evidence(
                instance_id=instance_id,
                text=unit.text,
                topic_key=unit.topic_key,
                polarity=unit.polarity,
                source_node_id=node_id,
                source_unit_id=unit_row["unit_id"],
                confidence=unit.confidence,
                salience=unit.salience,
                intent_type=unit.meta.get("intent_type"),
                horizon=unit.meta.get("horizon"),
                objective=unit.meta.get("objective"),
                refresh_scene=False,
            )
    await _advance_cognitive_cursor_on_connection(con, instance_id=instance_id, node_id=node_id, event_id=row["event_id"])
    if ambiguous:
        # Queue after the transaction commits in commit_message_cognition().
        # Persist the candidates on the commit so retries remain deterministic.
        await con.execute(
            """UPDATE aios.message_cognitive_commit
               SET summary=summary || $2::jsonb WHERE commit_id=$1""",
            commit_id,json.dumps({"ambiguous_sentences":ambiguous}),
        )
    return True


async def _advance_cognitive_cursor_on_connection(
    con: Any, *, instance_id: UUID, node_id: UUID, event_id: int | None
) -> None:
    await con.execute(
        """
        UPDATE aios.character_hud_readiness
        SET cognitive_ready_node_id=$2, cognitive_ready_event_id=$3,
            retrieval_ready_node_id=$2, retrieval_ready_event_id=$3, updated_at=now()
        WHERE instance_id=$1 AND source_head_node_id=$2
          AND (cognitive_ready_event_id IS NULL OR cognitive_ready_event_id <= $3)
        """,
        instance_id, node_id, event_id,
    )


async def _advance_cognitive_cursor(db: Database, *, instance_id: UUID, node_id: UUID, event_id: int | None) -> None:
    await db.execute(
        """
        UPDATE aios.character_hud_readiness
        SET cognitive_ready_node_id=$2, cognitive_ready_event_id=$3,
            retrieval_ready_node_id=$2, retrieval_ready_event_id=$3, updated_at=now()
        WHERE instance_id=$1 AND source_head_node_id=$2
          AND (cognitive_ready_event_id IS NULL OR cognitive_ready_event_id <= $3)
        """,
        instance_id, node_id, event_id,
    )


async def current_message_cognition(db: Database, *, instance_id: UUID, node_id: UUID | None) -> list[dict]:
    if node_id is None:
        return []
    rows = await db.fetch(
        """
        SELECT u.unit_id, u.claim_kind, u.text, u.topic_key, u.polarity,
               u.salience, u.confidence, u.meta, c.node_id, c.event_id
        FROM aios.message_cognitive_commit c
        JOIN aios.message_cognitive_unit u ON u.commit_id=c.commit_id
        WHERE c.instance_id=$1 AND c.node_id=$2 AND u.status='active'
        ORDER BY u.salience DESC, u.ordinal
        """,
        instance_id, node_id,
    )
    return [dict(row) for row in rows]


async def mark_enrichment_ready(db: Database, *, instance_id: UUID, node_id: UUID) -> None:
    row = await db.fetchrow("SELECT event_id FROM aios.dag_node WHERE node_id=$1", node_id)
    event_id = row["event_id"] if row else None
    await db.execute(
        """
        UPDATE aios.character_hud_readiness
        SET enrichment_ready_node_id=$2, enrichment_ready_event_id=$3, updated_at=now()
        WHERE instance_id=$1
        """,
        instance_id, node_id, event_id,
    )
    await db.execute(
        "UPDATE aios.message_cognitive_commit SET enrichment_completed_at=now() WHERE instance_id=$1 AND node_id=$2",
        instance_id, node_id,
    )



# Install the assertion/speech-act scope guard at the owning module boundary.
# This avoids a direct fast-path caller extracting with an unwrapped interpreter
# before an unrelated normalizer import mutates its effective version.
def _install_scope_policy() -> None:
    import sys
    from aios_app.epistemic.epistemic_scope import install_message_cognition_scope_guard
    install_message_cognition_scope_guard(sys.modules[__name__])


_install_scope_policy()
