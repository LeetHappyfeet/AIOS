from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from aios_app.db import Database

INTERPRETER_VERSION = "message-cognition-v1"
MAX_UNITS = 16

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"[a-z0-9_'-]+")
_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|cannot|can't|isn't|aren't|wasn't|weren't|doesn't|didn't|won't)\b",
    re.I,
)

_KIND_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "MEMORY",
        re.compile(
            r"\b(?:remember(?:s|ed|ing)?|recall(?:s|ed|ing)?|memory|memories|forgot|forget(?:s|ting)?|recognize(?:s|d|ing)?)\b",
            re.I,
        ),
    ),
    (
        "GOAL",
        re.compile(
            r"\b(?:want(?:s|ed|ing)?|intend(?:s|ed|ing)?|plan(?:s|ned|ning)?|goal|seek(?:s|ing)?|escape(?:s|d|ing)?|need(?:s|ed|ing)?|trying to|try to)\b",
            re.I,
        ),
    ),
    (
        "BELIEF",
        re.compile(
            r"\b(?:believe(?:s|d|ing)?|think(?:s|ing)?|thought|know(?:s|ing)?|knew|suspect(?:s|ed|ing)?|assume(?:s|d|ing)?|wonder(?:s|ed|ing)?|uncertain|maybe|perhaps)\b",
            re.I,
        ),
    ),
    (
        "RULE",
        re.compile(
            r"\b(?:must|mustn't|should|shouldn't|allowed|forbidden|required|cannot|can't|may not)\b",
            re.I,
        ),
    ),
    (
        "RELATIONSHIP",
        re.compile(
            r"\b(?:friend|enemy|ally|partner|assistant|sidekick|battle buddy|trust(?:s|ed|ing)?|distrust(?:s|ed|ing)?|help(?:s|ed|ing)? you|help(?:s|ed|ing)? me)\b",
            re.I,
        ),
    ),
    (
        "STATE",
        re.compile(
            r"\b(?:is|am|are|was|were|has|have|had|alive|dead|digital|data|inside|within|located|form|body|exist(?:s|ed|ing)?)\b",
            re.I,
        ),
    ),
)

_SALIENCE_RE = re.compile(
    r"\b(?:remember(?:s|ed|ing)?|recall(?:s|ed|ing)?|know(?:s|ing)?|knew|believe(?:s|d|ing)?|"
    r"think(?:s|ing)?|thought|want(?:s|ed|ing)?|need(?:s|ed|ing)?|plan(?:s|ned|ning)?|"
    r"intend(?:s|ed|ing)?|goal|must|cannot|can't|alive|dead|digital|data|computer|world|body|"
    r"form|escape(?:s|d|ing)?|help(?:s|ed|ing)?|partner|assistant|friend|enemy|trust(?:s|ed|ing)?|"
    r"uncertain|maybe|real|exist(?:s|ed|ing)?|location|inside|outside|moved|arrived|left|gave|took)\b",
    re.I,
)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "hers", "him", "his", "i", "in", "is", "it",
    "its", "me", "my", "of", "on", "or", "our", "she", "that", "the", "their", "them",
    "they", "this", "to", "was", "we", "were", "with", "you", "your",
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


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.split(text or "") if part.strip()]


def _kind(text: str) -> str:
    for kind, pattern in _KIND_RULES:
        if pattern.search(text):
            return kind
    return "EVENT"


def _topic_key(text: str, *, character_id: str, speaker_id: str | None) -> str:
    tokens = [
        token
        for token in _WORD_RE.findall(text.lower())
        if len(token) >= 3
        and token not in _STOPWORDS
        and token not in {"not", "never", "cannot", "can't"}
    ]
    preferred: list[str] = []
    for value in (character_id, speaker_id):
        clean = re.sub(r"[^a-z0-9_'-]+", " ", (value or "").lower()).strip()
        if clean:
            preferred.extend(part for part in clean.split() if part not in preferred)
    for token in tokens:
        if token not in preferred:
            preferred.append(token)
        if len(preferred) >= 5:
            break
    return ":".join(preferred[:5]) or "message"


def _score(
    text: str,
    *,
    index: int,
    total: int,
    character_id: str,
    speaker_id: str | None,
) -> float:
    score = 0.15
    hits = len(_SALIENCE_RE.findall(text))
    score += min(0.50, hits * 0.12)
    lower = text.lower()
    if character_id and character_id.lower() in lower:
        score += 0.12
    if speaker_id and speaker_id.lower() in lower:
        score += 0.08
    if index < 2 or index >= max(0, total - 2):
        score += 0.06
    if "?" in text:
        score += 0.04
    if len(text) > 320:
        score -= 0.05
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

    ranked: list[tuple[float, int, CognitiveUnit]] = []
    seen: set[str] = set()
    for index, sentence in enumerate(sentences):
        normalized = " ".join(_WORD_RE.findall(sentence.lower()))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        salience = _score(
            sentence,
            index=index,
            total=len(sentences),
            character_id=character_id,
            speaker_id=speaker_id,
        )
        if len(sentences) > 4 and salience < 0.27:
            continue
        kind = _kind(sentence)
        polarity = -1 if _NEGATION_RE.search(sentence) else 1
        unit = CognitiveUnit(
            text=sentence,
            claim_kind=kind,
            topic_key=_topic_key(
                sentence,
                character_id=character_id,
                speaker_id=speaker_id,
            ),
            polarity=polarity,
            salience=salience,
            confidence=max(0.45, min(0.90, 0.48 + salience * 0.42)),
            meta={
                "message_scope": True,
                "speaker_id": speaker_id,
                "speaker_role": speaker_role,
                "viewpoint_id": viewpoint_id,
                "sentence_index": index,
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
