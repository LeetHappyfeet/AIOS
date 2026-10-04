"""Bounded historical candidate hygiene; preserve original mentions and source receipts."""
from __future__ import annotations

from .collector import normalize_label


async def retire_invalid_candidates_once(db, *, limit: int = 16) -> int:
    """Retire legacy non-linguistic candidate labels without deleting provenance.

    Whitelisted/registered human-maintained topics are not implicitly retired.
    A retired label cannot reactivate through the collector because both original
    source labeling and upsert now apply the same normalization policy.
    """
    rows=await db.fetch(
        """SELECT topic_id,display_label
           FROM aios.knowledge_topic
           WHERE status='candidate'
           ORDER BY updated_at,topic_id LIMIT $1""",
        max(1,min(int(limit)*12,512)))
    bad=[r["topic_id"] for r in rows if normalize_label(r["display_label"]) is None][:limit]
    if not bad:
        return 0
    return len(await db.fetch(
        """UPDATE aios.knowledge_topic SET status='retired',
                  vector_revision=vector_revision+1,graph_revision=graph_revision+1,
                  updated_at=now()
           WHERE topic_id=ANY($1::uuid[]) AND status='candidate'
           RETURNING topic_id""",bad))
