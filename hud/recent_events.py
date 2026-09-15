from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

_PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]{0,48}):\s*(.+)$", re.S)
_WORD_RE = re.compile(r"[A-Za-z0-9_'-]+")
_PRONOUN_PREFIXES = {"he", "she", "they", "it", "his", "her", "hers", "their", "theirs", "its"}


def _norm(text: str) -> str:
    return " ".join(token.lower() for token in _WORD_RE.findall(text))


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
