from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

_PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]{0,48}):\s*(.+)$", re.S)
_WORD_RE = re.compile(r"[A-Za-z0-9_'-]+")
_PRONOUN_PREFIXES = {"he", "she", "they", "it", "his", "her", "hers", "their", "theirs", "its"}


def _norm(text: str) -> str:
    return " ".join(token.lower() for token in _WORD_RE.findall(text))


def normalized_text(text: str) -> str:
    """Normalize presentation text for deterministic cross-section deduplication."""
    return _norm(text)


def _bounded(text: str, max_chars: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rstrip()
    # Prefer a natural sentence boundary when one exists reasonably near the cap.
    boundary = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "))
    if boundary >= max_chars // 2:
        return clipped[: boundary + 1].rstrip()
    return clipped.rstrip(" ,;:-") + "…"


def narrative_text(event: Mapping[str, Any]) -> str:
    """Return presentation text without pretending a narrative subject is a speaker."""
    text = str(event.get("text") or event.get("message_text") or "").strip()
    if not text:
        return ""

    # Structured semantic events may already expose SPO fields. Prefer them when
    # they form a useful bounded sentence; otherwise preserve the admitted text.
    subject = str(event.get("subject_norm") or "").strip()
    predicate = str(event.get("predicate_norm") or "").strip()
    obj = str(event.get("object_norm") or "").strip()
    if subject and predicate and obj and predicate not in {"be_definition_of"}:
        return f"{subject} {predicate.replace('_', ' ')} {obj}."

    # Older cognition encoded narrative ownership as `Alex: He opened...`.
    # That colon is speaker syntax, but these are events, not dialogue. Strip it.
    match = _PREFIX_RE.match(text)
    if match:
        prefix, body = match.groups()
        # Always remove pronoun pseudo-speakers such as `His:`. For named
        # semantic events also remove the prefix: source messages retain their
        # speaker metadata and are rendered separately by render_text.py.
        if not event.get("message_text") or prefix.lower() in _PRONOUN_PREFIXES:
            return body.strip()
    return text


def project_recent_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate source/semantic presentations without deleting evidence."""
    rows = [dict(event) for event in events]
    semantic_source_ids = {
        str(event.get("source_node_id"))
        for event in rows
        if not event.get("message_text") and event.get("source_node_id")
    }

    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in rows:
        # If a bounded semantic projection exists for exactly this source node,
        # do not also spend HUD budget on the full raw source message.
        node_id = event.get("node_id")
        if event.get("message_text") and node_id and str(node_id) in semantic_source_ids:
            continue

        text = narrative_text(event)
        key = _norm(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        event["text"] = text
        projected.append(event)
    return projected



def project_scene_change(
    events: Iterable[Mapping[str, Any]],
    *,
    max_chars: int = 480,
) -> str | None:
    """Project the newest source event into a compact scene-state change.

    Scene state must never persist an unbounded source transcript. Prefer a
    semantic event tied to the newest source node, then structured/narrative
    event text, and use the raw source message only as a bounded last resort.
    """
    rows = [dict(event) for event in events]
    if not rows:
        return None

    newest = rows[0]
    newest_node = newest.get("node_id") or newest.get("source_node_id")
    candidates = rows
    if newest_node:
        node_key = str(newest_node)
        same_source = [
            event
            for event in rows
            if str(event.get("source_node_id") or event.get("node_id") or "") == node_key
        ]
        if same_source:
            candidates = same_source

    # Semantic rows do not carry message_text. They are the preferred scene
    # projection when cognition has caught up with the source turn.
    for event in candidates:
        if event.get("message_text"):
            continue
        text = narrative_text(event)
        if text:
            return _bounded(text, max_chars)

    # Some current-event rows carry both a bounded semantic text and the raw
    # source message. Prefer that explicit text before falling back to transcript.
    for event in candidates:
        text = str(event.get("text") or "").strip()
        if text:
            return _bounded(narrative_text({**event, "message_text": None}), max_chars)

    raw = narrative_text(newest)
    return _bounded(raw, max_chars) if raw else None
