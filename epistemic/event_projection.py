from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


_LOW_INFO = {"", "_", "*", "it", "this", "that", "something", "anything", "everything", "nothing"}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _informative(value: Any) -> bool:
    text = _clean(value).lower()
    return text not in _LOW_INFO and len(text) > 1


def _sentence(subject: str, predicate: str, obj: str) -> str:
    subject, predicate, obj = _clean(subject), _clean(predicate), _clean(obj)
    if not _informative(subject):
        return ""
    if predicate in {"be", "identity"} and _informative(obj):
        return f"{subject} was {obj}."
    if _informative(predicate) and _informative(obj):
        return f"{subject} {predicate} {obj}."
    if _informative(predicate):
        return f"{subject} {predicate}."
    return ""


def project_semantic_event(
    event: dict[str, Any],
    members: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic cognition-owned read model for one occurrence.

    The semantic_event remains identity/provenance. This projection is disposable:
    it can be rebuilt whenever membership changes and never becomes source truth.
    """
    rows = [dict(row) for row in members]
    if not rows:
        return {"semantic_event_id": event.get("semantic_event_id"), "text": "", "members": []}

    def quality(row: dict[str, Any]) -> tuple[float, int, int, str]:
        rel = row.get("relevance") or {}
        subject = _clean(row.get("subject_norm"))
        obj = _clean(row.get("object_norm"))
        richness = int(_informative(subject)) + int(_informative(obj))
        return (
            float(rel.get("total") or 0.0),
            richness,
            len(_clean(row.get("text"))),
            str(row.get("proposition_id") or ""),
        )

    ordered = sorted(rows, key=quality, reverse=True)
    subjects = [_clean(row.get("subject_norm")) for row in rows if _informative(row.get("subject_norm"))]
    participants = [value for value, _ in Counter(subjects).most_common()]
    objects = []
    for row in ordered:
        value = _clean(row.get("object_norm"))
        if _informative(value) and value not in objects:
            objects.append(value)

    # Prefer an action-like member as the event spine. State/identity members are
    # supporting conditions; they should not replace a richer occurrence.
    action = next(
        (
            row for row in ordered
            if _clean(row.get("predicate_norm")).lower() not in {"be", "identity", "have"}
            and _informative(row.get("subject_norm"))
        ),
        ordered[0],
    )
    action_text = _sentence(
        _clean(action.get("subject_norm")),
        _clean(action.get("predicate_norm")),
        _clean(action.get("object_norm")),
    )

    supporting: list[str] = []
    for row in ordered:
        if row is action:
            continue
        predicate = _clean(row.get("predicate_norm")).lower()
        if predicate not in {"be", "identity", "have"}:
            continue
        rendered = _sentence(
            _clean(row.get("subject_norm")),
            predicate,
            _clean(row.get("object_norm")),
        )
        if rendered and rendered.lower() != action_text.lower() and rendered not in supporting:
            supporting.append(rendered)
        if len(supporting) >= 2:
            break

    fallback = _clean(action.get("text"))
    text = action_text or fallback
    if supporting and text:
        text = " ".join([text, *supporting])

    direct_vectors = [
        float((row.get("relevance") or {}).get("vector_similarity"))
        for row in rows
        if (row.get("relevance") or {}).get("vector_similarity") is not None
    ]
    return {
        "semantic_event_id": event.get("semantic_event_id"),
        "world_id": event.get("world_id"),
        "timeline_id": event.get("timeline_id"),
        "dag_node_id": event.get("dag_node_id"),
        "confidence": float(event.get("event_confidence") or 0.0),
        "text": text,
        "participants": participants,
        "objects": objects,
        "primary_proposition_id": action.get("proposition_id"),
        "member_proposition_ids": [row.get("proposition_id") for row in rows],
        "best_vector_similarity": max(direct_vectors) if direct_vectors else None,
        "members": [
            {
                "proposition_id": row.get("proposition_id"),
                "subject": row.get("subject_norm"),
                "predicate": row.get("predicate_norm"),
                "object": row.get("object_norm"),
                "text": row.get("text"),
            }
            for row in ordered
        ],
        "projection_version": "semantic-event-projection-v1",
    }
