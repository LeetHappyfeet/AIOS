from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from aios_app.db import Database

INTERPRETER_VERSION = "message-cognition-v2"
MAX_UNITS = 12

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"[a-z0-9_'-]+")
_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|cannot|can't|isn't|aren't|wasn't|weren't|doesn't|didn't|won't)\b",
    re.I,
)
_QUESTION_RE = re.compile(r"\?\s*$")
_CAUSAL_DESIRE_RE = re.compile(
    r"\b(?:made|makes|making|caused|causes|causing)\s+(?:me|you|him|her|them|us|[a-z0-9_-]+)\s+want\b",
    re.I,
)

# The fast interpreter is deliberately conservative. It recognizes grammatical
# shapes instead of treating a trigger word anywhere in a sentence as cognition.
_SUBJECT = r"(?P<subject>I|you|she|he|they|we|[A-Za-z][A-Za-z0-9_-]{1,48})"
_GOAL_RE = re.compile(
    rf"\b{_SUBJECT}\s+(?P<verb>want(?:s|ed)?|intend(?:s|ed)?|plan(?:s|ned)?|need(?:s|ed)?|"
    rf"seek(?:s|ed)?|tr(?:y|ies|ied)|is\s+trying|was\s+trying|decide(?:s|d)?|resolve(?:s|d)?|prepare(?:s|d)?)"
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
    rf"\b{_SUBJECT}\s+(?P<verb>must|mustn't|should|shouldn't|cannot|can't|may\s+not|is\s+required\s+to|"
    rf"is\s+allowed\s+to|is\s+forbidden\s+to)\s+(?P<object>[^.!?]{{2,220}})",
    re.I,
)
_RELATIONSHIP_RE = re.compile(
    r"\b(?:friend|enemy|ally|partner|assistant|sidekick|battle buddy|trust(?:s|ed|ing)?|"
    r"distrust(?:s|ed|ing)?|offer(?:s|ed|ing)?\s+to\s+help|help(?:s|ed|ing)?\s+(?:me|you|her|him|them)|"
    r"work(?:s|ed|ing)?\s+(?:with|for))\b",
    re.I,
)
_STATE_TERMS_RE = re.compile(
    r"\b(?:alive|dead|digital|data|computer|body|form|real|physical|trapped|free|inside|outside|within|"
    r"located|location|exists?|existing|conscious|awake|asleep|injured|armed|powered|human|artificial)\b",
    re.I,
)
_STATE_RE = re.compile(
    rf"\b{_SUBJECT}\s+(?P<verb>is|am|are|was|were|has|have|had|exists?|became|becomes|remains?)\s+"
    r"(?P<object>[^.!?]{2,220})",
    re.I,
)
_EVENT_RE = re.compile(
    r"\b(?:arrived|left|moved|entered|escaped|attacked|fought|gave|took|opened|closed|activated|"
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
    "MEMORY": "durable",
    "BELIEF": "until_contradicted",
    "GOAL": "session",
    "RULE": "until_contradicted",
    "RELATIONSHIP": "until_contradicted",
    "STATE": "until_changed",
    "EVENT": "turn",
}

_KIND_BASE_SALIENCE = {
    "MEMORY": 0.78,
    "BELIEF": 0.72,
    "GOAL": 0.82,
    "RULE": 0.74,
    "RELATIONSHIP": 0.70,
    "STATE": 0.68,
    "EVENT": 0.48,
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
class ParsedCandidate:
    kind: str
    subject_text: str | None
    predicate: str
    object_text: str
    confidence: float
    reason: str


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
    # Common character identifiers look like Shego_001. The lexical stem is a
    # useful explicit-name alias, while short numeric suffixes are not.
    stem = re.sub(r"[_-]?\d+$", "", clean).strip("_- ")
    if stem:
        aliases.add(stem)
    return aliases


def _same_identity(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return bool(_identity_aliases(left) & _identity_aliases(right))


def _resolve_subject(
    subject: str | None,
    *,
    character_id: str,
    speaker_id: str | None,
    viewpoint_id: str | None,
) -> tuple[str | None, bool]:
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
        # In ordinary dialogue, second person points at the active character
        # when somebody else is speaking, but at the other participant when the
        # character is the speaker.
        if speaker_id and not _same_identity(speaker_id, character_id):
            return character_id, True
        return "other", False
    if raw in {"she", "he", "they"}:
        # Narrative pronouns are resolved only when the message explicitly has
        # the active character viewpoint; otherwise keep them unowned.
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


def _canonical_text(candidate: ParsedCandidate, *, owner: str | None) -> str:
    subject = _canonical_subject(owner, candidate.subject_text)
    obj = _clean_object(candidate.object_text)
    predicate = candidate.predicate.lower().strip()

    if candidate.kind == "MEMORY":
        return f"{subject} remembers {obj}."
    if candidate.kind == "BELIEF":
        if predicate in {"is", "was", "seems", "seemed"} and obj.startswith(("uncertain", "unsure", "confused")):
            return f"{subject} is {obj}."
        return f"{subject} {predicate} {obj}."
    if candidate.kind == "GOAL":
        if predicate.startswith(("decide", "resolve", "prepare")):
            return f"{subject} intends {obj}."
        return f"{subject} {predicate} {obj}."
    if candidate.kind == "RULE":
        return f"{subject} {predicate} {obj}."
    if candidate.kind == "STATE":
        return f"{subject} {predicate} {obj}."
    return f"{subject}: {obj}." if candidate.subject_text else f"{obj}."


def _parse_sentence(sentence: str) -> ParsedCandidate | None:
    # Questions describe conversational pressure, not settled cognition. This
    # single rule eliminates most false goals such as "what do you want?" and
    # "you want me to be your assistant?".
    if _QUESTION_RE.search(sentence):
        return None

    match = _MEMORY_RE.search(sentence)
    if match:
        return ParsedCandidate(
            "MEMORY", match.group("subject"), match.group("verb"),
            match.group("object"), 0.94, "memory_predicate",
        )

    match = _BELIEF_RE.search(sentence)
    if match:
        return ParsedCandidate(
            "BELIEF", match.group("subject"), match.group("verb"),
            match.group("object"), 0.92, "belief_predicate",
        )

    match = _UNCERTAIN_RE.search(sentence)
    if match:
        obj = f"{match.group('object')}{match.group('tail') or ''}"
        return ParsedCandidate(
            "BELIEF", match.group("subject"), match.group("verb"),
            obj, 0.90, "uncertainty_state",
        )

    match = _RULE_RE.search(sentence)
    if match:
        return ParsedCandidate(
            "RULE", match.group("subject"), match.group("verb"),
            match.group("object"), 0.93, "deontic_predicate",
        )

    if not _CAUSAL_DESIRE_RE.search(sentence):
        match = _GOAL_RE.search(sentence)
        if match:
            obj = match.group("object")
            # "want X to do Y" is a desire about another actor, not necessarily
            # the subject's own intended action. Keep only direct infinitive or
            # noun-goal complements here; archaeology can recover subtler cases.
            return ParsedCandidate(
                "GOAL", match.group("subject"), match.group("verb"),
                obj, 0.91, "goal_predicate",
            )

    # Relationship statements are useful even when asserted by another speaker;
    # they are observations available to the character rather than private goals.
    if _RELATIONSHIP_RE.search(sentence):
        words = _WORD_RE.findall(sentence)
        subject = words[0] if words else None
        return ParsedCandidate(
            "RELATIONSHIP", subject, "relates", sentence, 0.82,
            "relationship_predicate",
        )

    match = _STATE_RE.search(sentence)
    if match and _STATE_TERMS_RE.search(match.group("object")):
        return ParsedCandidate(
            "STATE", match.group("subject"), match.group("verb"),
            match.group("object"), 0.86, "bounded_state_predicate",
        )

    if _EVENT_RE.search(sentence):
        words = _WORD_RE.findall(sentence)
        subject = words[0] if words else None
        return ParsedCandidate(
            "EVENT", subject, "event", sentence, 0.70,
            "event_predicate",
        )

    return None


def _topic_key(
    text: str,
    *,
    character_id: str,
    owner: str | None,
    kind: str,
) -> str:
    tokens = [
        token
        for token in _WORD_RE.findall(text.lower())
        if len(token) >= 3
        and token not in _STOPWORDS
        and token not in {"not", "never", "cannot", "can't"}
    ]
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


def _score_candidate(
    candidate: ParsedCandidate,
    *,
    character_owned: bool,
    index: int,
    total: int,
) -> float:
    score = _KIND_BASE_SALIENCE[candidate.kind]
    if character_owned:
        score += 0.10
    if index >= max(0, total - 2):
        score += 0.03
    return max(0.0, min(1.0, score))


def interpret_message(
    text: str,
    *,
    character_id: str,
    speaker_id: str | None,
    speaker_role: str | None,
    viewpoint_id: str | None,
) -> list[CognitiveUnit]:
    sentences = _sentences(text)
    if not sentences:
        return []

    perspective_id = viewpoint_id or speaker_id
    ranked: list[tuple[float, int, CognitiveUnit]] = []
    seen_sources: set[str] = set()
    seen_semantics: set[tuple[str, str, int]] = set()

    for index, sentence in enumerate(sentences):
        normalized_source = " ".join(_WORD_RE.findall(sentence.lower()))
        if not normalized_source or normalized_source in seen_sources:
            continue
        seen_sources.add(normalized_source)

        candidate = _parse_sentence(sentence)
        if candidate is None:
            continue

        owner, character_owned = _resolve_subject(
            candidate.subject_text,
            character_id=character_id,
            speaker_id=speaker_id,
            viewpoint_id=viewpoint_id,
        )

        # Private cognition belongs in the active character HUD only when the
        # grammatical subject resolves to that character. Statements by another
        # speaker remain useful only for externally observable kinds.
        if candidate.kind in {"MEMORY", "BELIEF", "GOAL", "RULE"} and not character_owned:
            continue

        canonical = _canonical_text(candidate, owner=owner)
        polarity = -1 if _NEGATION_RE.search(sentence) else 1
        topic_key = _topic_key(
            canonical,
            character_id=character_id,
            owner=owner,
            kind=candidate.kind,
        )
        semantic_key = (candidate.kind, topic_key, polarity)
        if semantic_key in seen_semantics:
            continue
        seen_semantics.add(semantic_key)

        salience = _score_candidate(
            candidate,
            character_owned=character_owned,
            index=index,
            total=len(sentences),
        )
        confidence = max(0.50, min(0.99, candidate.confidence))
        unit = CognitiveUnit(
            text=canonical,
            claim_kind=candidate.kind,
            topic_key=topic_key,
            polarity=polarity,
            salience=salience,
            confidence=confidence,
            meta={
                "message_scope": True,
                "speaker_id": speaker_id,
                "speaker_role": speaker_role,
                "viewpoint_id": viewpoint_id,
                "perspective_id": perspective_id,
                "semantic_owner": owner,
                "character_owned": character_owned,
                "sentence_index": index,
                "source_text": sentence[:500],
                "predicate": candidate.predicate.lower(),
                "object": _clean_object(candidate.object_text),
                "parse_reason": candidate.reason,
                "persistence": _PERSISTENCE[candidate.kind],
                "parse_confidence": confidence,
                "epistemic_confidence": 0.72 if character_owned else 0.58,
            },
        )
        ranked.append((salience, index, unit))

    ranked.sort(key=lambda value: (-value[0], value[1]))
    selected = ranked[:MAX_UNITS]
    selected.sort(key=lambda value: value[1])
    return [unit for _, _, unit in selected]


async def _reconcile_unit(
    db: Database,
    *,
    instance_id: UUID,
    unit_id: UUID,
    claim_kind: str,
    topic_key: str,
    polarity: int,
) -> UUID | None:
    if claim_kind not in {"BELIEF", "STATE", "GOAL", "RELATIONSHIP", "RULE"}:
        return None
    previous = await db.fetchrow(
        """
        SELECT u.unit_id, u.polarity
        FROM aios.message_cognitive_unit u
        JOIN aios.message_cognitive_commit c ON c.commit_id=u.commit_id
        WHERE c.instance_id=$1
          AND u.unit_id<>$2
          AND u.claim_kind=$3
          AND u.topic_key=$4
          AND u.status='active'
        ORDER BY c.event_id DESC NULLS LAST, u.created_at DESC
        LIMIT 1
        """,
        instance_id,
        unit_id,
        claim_kind,
        topic_key,
    )
    if not previous or int(previous["polarity"] or 1) == polarity:
        return None
    previous_id = previous["unit_id"]
    await db.execute(
        "UPDATE aios.message_cognitive_unit SET status='superseded' WHERE unit_id=$1",
        previous_id,
    )
    await db.execute(
        """
        UPDATE aios.message_cognitive_unit
        SET supersedes_unit_id=$2,
            meta=meta || jsonb_build_object('reconciled_polarity_flip', true)
        WHERE unit_id=$1
        """,
        unit_id,
        previous_id,
    )
    return previous_id


async def commit_message_cognition(
    db: Database,
    *,
    instance_id: UUID,
    node_id: UUID,
) -> bool:
    """Commit bounded SQL-only cognition for one source message."""
    row = await db.fetchrow(
        """
        SELECT
            dn.node_id, dn.timeline_id, dn.event_id, dn.message_text,
            dn.speaker_id, dn.speaker_role::text AS speaker_role,
            COALESCE(NULLIF(dn.viewpoint_id,''), NULLIF(dn.payload->>'viewpoint_id','')) AS viewpoint_id,
            ci.character_id
        FROM aios.dag_node dn
        JOIN aios.character_instance ci ON ci.instance_id=$1
        WHERE dn.node_id=$2
        """,
        instance_id,
        node_id,
    )
    if not row:
        return False

    text = str(row["message_text"] or "").strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = await db.fetchrow(
        """
        SELECT commit_id, source_text_hash, interpreter_version
        FROM aios.message_cognitive_commit
        WHERE instance_id=$1 AND node_id=$2
        """,
        instance_id,
        node_id,
    )
    if (
        existing
        and existing["source_text_hash"] == digest
        and existing["interpreter_version"] == INTERPRETER_VERSION
    ):
        await _advance_cognitive_cursor(
            db,
            instance_id=instance_id,
            node_id=node_id,
            event_id=row["event_id"],
        )
        return True

    units = interpret_message(
        text,
        character_id=str(row["character_id"]),
        speaker_id=row["speaker_id"],
        speaker_role=row["speaker_role"],
        viewpoint_id=row["viewpoint_id"],
    )
    summary = {
        "unit_count": len(units),
        "kinds": sorted({unit.claim_kind for unit in units}),
        "participants": [
            value for value in (row["speaker_id"], row["character_id"]) if value
        ],
        "bounded": True,
        "max_units": MAX_UNITS,
        "interpreter_version": INTERPRETER_VERSION,
    }

    commit_row = await db.execute_returning_row(
        """
        INSERT INTO aios.message_cognitive_commit (
            instance_id, node_id, timeline_id, event_id, character_id,
            speaker_id, speaker_role, viewpoint_id, interpreter_version,
            source_text_hash, summary, committed_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,now())
        ON CONFLICT (instance_id, node_id) DO UPDATE
        SET timeline_id=EXCLUDED.timeline_id,
            event_id=EXCLUDED.event_id,
            character_id=EXCLUDED.character_id,
            speaker_id=EXCLUDED.speaker_id,
            speaker_role=EXCLUDED.speaker_role,
            viewpoint_id=EXCLUDED.viewpoint_id,
            interpreter_version=EXCLUDED.interpreter_version,
            source_text_hash=EXCLUDED.source_text_hash,
            summary=EXCLUDED.summary,
            committed_at=now(),
            enrichment_completed_at=NULL
        RETURNING commit_id
        """,
        instance_id,
        node_id,
        row["timeline_id"],
        row["event_id"],
        row["character_id"],
        row["speaker_id"],
        row["speaker_role"],
        row["viewpoint_id"],
        INTERPRETER_VERSION,
        digest,
        json.dumps(summary),
    )
    commit_id = commit_row["commit_id"]
    await db.execute(
        "DELETE FROM aios.message_cognitive_unit WHERE commit_id=$1",
        commit_id,
    )

    for ordinal, unit in enumerate(units):
        unit_row = await db.execute_returning_row(
            """
            INSERT INTO aios.message_cognitive_unit (
                commit_id, ordinal, claim_kind, text, topic_key, polarity,
                salience, confidence, status, meta
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active',$9::jsonb)
            RETURNING unit_id
            """,
            commit_id,
            ordinal,
            unit.claim_kind,
            unit.text,
            unit.topic_key,
            unit.polarity,
            unit.salience,
            unit.confidence,
            json.dumps(unit.meta),
        )
        await _reconcile_unit(
            db,
            instance_id=instance_id,
            unit_id=unit_row["unit_id"],
            claim_kind=unit.claim_kind,
            topic_key=unit.topic_key,
            polarity=unit.polarity,
        )

    await _advance_cognitive_cursor(
        db,
        instance_id=instance_id,
        node_id=node_id,
        event_id=row["event_id"],
    )
    return True


async def _advance_cognitive_cursor(
    db: Database,
    *,
    instance_id: UUID,
    node_id: UUID,
    event_id: int | None,
) -> None:
    await db.execute(
        """
        UPDATE aios.character_hud_readiness
        SET cognitive_ready_node_id=$2,
            cognitive_ready_event_id=$3,
            retrieval_ready_node_id=$2,
            retrieval_ready_event_id=$3,
            updated_at=now()
        WHERE instance_id=$1
        """,
        instance_id,
        node_id,
        event_id,
    )


async def current_message_cognition(
    db: Database,
    *,
    instance_id: UUID,
    node_id: UUID | None,
) -> list[dict]:
    if node_id is None:
        return []
    rows = await db.fetch(
        """
        SELECT u.unit_id, u.claim_kind, u.text, u.topic_key, u.polarity,
               u.salience, u.confidence, u.meta, c.node_id, c.event_id
        FROM aios.message_cognitive_commit c
        JOIN aios.message_cognitive_unit u ON u.commit_id=c.commit_id
        WHERE c.instance_id=$1
          AND c.node_id=$2
          AND u.status='active'
        ORDER BY u.salience DESC, u.ordinal
        """,
        instance_id,
        node_id,
    )
    return [dict(row) for row in rows]


async def mark_enrichment_ready(
    db: Database,
    *,
    instance_id: UUID,
    node_id: UUID,
) -> None:
    row = await db.fetchrow(
        "SELECT event_id FROM aios.dag_node WHERE node_id=$1",
        node_id,
    )
    event_id = row["event_id"] if row else None
    await db.execute(
        """
        UPDATE aios.character_hud_readiness
        SET enrichment_ready_node_id=$2,
            enrichment_ready_event_id=$3,
            updated_at=now()
        WHERE instance_id=$1
        """,
        instance_id,
        node_id,
        event_id,
    )
    await db.execute(
        """
        UPDATE aios.message_cognitive_commit
        SET enrichment_completed_at=now()
        WHERE instance_id=$1 AND node_id=$2
        """,
        instance_id,
        node_id,
    )
