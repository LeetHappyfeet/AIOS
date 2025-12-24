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
        SELECT node_id, created_at, speaker_id, speaker_role, message_text
        FROM aios.dag_node
        WHERE timeline_id = $1
          AND message_text IS NOT NULL
          AND length(message_text) > 0
        ORDER BY created_at DESC, node_id DESC
        LIMIT $2
        """,
        timeline_id, limit
    )

    matches: List[MemoryMatch] = []
    # Reverse so the chunk reads old->new
    for r in reversed(rows):
        speaker = r["speaker_id"] or "unknown"
        role = r["speaker_role"] or "other"
        txt = r["message_text"]
        content = f"[{role}:{speaker}] {txt}"
        matches.append(MemoryMatch(content=content, score=0.0, meta={"node_id": int(r["node_id"])}))
    return matches


async def pick_latest_timeline_for_character(
    db: Database,
    *,
    character_id: str,
    user_name: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> Optional[UUID]:
    # Safety default: if user_name isn't supplied, we pick the most recent timeline.
    # In production you SHOULD pass user_name (and ideally session_id) to avoid cross-user leakage.
    if user_name is not None:
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
            character_id, user_name, scope_key
        )
    else:
        row = await db.fetchrow(
            """
            SELECT timeline_id
            FROM aios.timeline
            WHERE character_id = $1
              AND ($2::text IS NULL OR scope_key = $2)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            character_id, scope_key
        )

    return row["timeline_id"] if row else None
