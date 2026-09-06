from uuid import UUID
from typing import List, Optional

from .db import Database
from .models import MemoryMatch


async def recent_nodes_as_memory(
    db: Database,
    *,
    timeline_id: UUID,
    limit: int = 8,
) -> List[MemoryMatch]:
    rows = await db.fetch(
        """
        SELECT dn.node_id, dn.created_at, dn.speaker_id, dn.speaker_role, dn.message_text
        FROM aios.dag_node dn
        JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE dn.timeline_id = $1
          AND ie.superseded_at IS NULL
          AND message_text IS NOT NULL
          AND length(message_text) > 0
        ORDER BY dn.event_id DESC
        LIMIT $2
        """,
        timeline_id,
        limit,
    )

    matches: List[MemoryMatch] = []
    for row in reversed(rows):
        speaker = row["speaker_id"] or "unknown"
        role = row["speaker_role"] or "other"
        content = f"[{role}:{speaker}] {row['message_text']}"
        matches.append(
            MemoryMatch(
                content=content,
                score=0.0,
                meta={"node_id": str(row["node_id"])},
            )
        )
    return matches


async def pick_latest_timeline_for_character(
    db: Database,
    *,
    character_id: str,
    user_name: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> Optional[UUID]:
    """Legacy resolver that refuses ambiguous cross-user character memory."""
    if user_name is None:
        return None

    row = await db.fetchrow(
        """
        SELECT timeline_id
        FROM aios.timeline
        WHERE character_id = $1
          AND user_name = $2
          AND ($3::text IS NULL OR scope_key = $3)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        character_id,
        user_name,
        scope_key,
    )
    return row["timeline_id"] if row else None


async def timeline_for_instance(
    db: Database,
    *,
    instance_id: UUID,
) -> Optional[UUID]:
    """Resolve the exact experiential timeline for a live runtime instance."""
    row = await db.fetchrow(
        """
        SELECT timeline_id
        FROM aios.character_runtime_state
        WHERE instance_id = $1
        """,
        instance_id,
    )
    return row["timeline_id"] if row else None
