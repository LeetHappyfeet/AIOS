from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from aios_app.epistemic.event_projection import project_semantic_event


def project_semantic_episode(
    episode: dict[str, Any],
    events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build a disposable cognition-facing projection of an episode."""
    rows = [dict(row) for row in events]
    rows.sort(key=lambda row: (
        int(row.get("episode_ordinal") or 0),
        str(row.get("semantic_event_id") or ""),
    ))
    event_texts: list[str] = []
    participants: list[str] = []
    proposition_ids: list[Any] = []
    event_ids: list[Any] = []
    best_vector = None
    for row in rows:
        members = list(row.get("members") or [])
        projection = project_semantic_event(row, members)
        text = str(projection.get("text") or "").strip()
        if text and text not in event_texts:
            event_texts.append(text)
        participants.extend(projection.get("participants") or [])
        proposition_ids.extend(projection.get("member_proposition_ids") or [])
        event_ids.append(row.get("semantic_event_id"))
        vector = projection.get("best_vector_similarity")
        if vector is not None:
            best_vector = float(vector) if best_vector is None else max(best_vector, float(vector))

    # Keep this deterministic and compact.  It is a read model, not new truth.
    text = " ".join(event_texts[:6])
    ordered_participants = [name for name, _ in Counter(participants).most_common()]
    return {
        "semantic_episode_id": episode.get("semantic_episode_id"),
        "world_id": episode.get("world_id"),
        "timeline_id": episode.get("timeline_id"),
        "text": text,
        "semantic_event_ids": event_ids,
        "member_proposition_ids": proposition_ids,
        "participants": ordered_participants,
        "best_vector_similarity": best_vector,
        "projection_version": "semantic-episode-projection-v1",
    }
