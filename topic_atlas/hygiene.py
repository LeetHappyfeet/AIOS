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
             AND (display_label ~* '^[a-f0-9]{32,64}
    bad=[r["topic_id"] for r in rows if normalize_label(r["display_label"]) is None][:limit]
    if not bad:
        return 0
    async with db.connection() as con:
        async with con.transaction():
            retired=await con.fetch(
                """UPDATE aios.knowledge_topic SET status='retired',
                          vector_revision=vector_revision+1,graph_revision=graph_revision+1,
                          updated_at=now()
                   WHERE topic_id=ANY($1::uuid[]) AND status='candidate'
                   RETURNING topic_id""",bad)
            ids=[r["topic_id"] for r in retired]
            if ids:
                # Graphs that referenced a newly retired neighbor must lose
                # their navigation edge in the next acknowledged projection.
                await con.execute(
                    """UPDATE aios.knowledge_topic t
                       SET graph_revision=graph_revision+1,updated_at=now()
                       WHERE t.status<>'retired' AND t.topic_id IN (
                         SELECT r.target_topic_id FROM aios.knowledge_topic_relation r
                          WHERE r.source_topic_id=ANY($1::uuid[])
                         UNION
                         SELECT r.source_topic_id FROM aios.knowledge_topic_relation r
                          WHERE r.target_topic_id=ANY($1::uuid[])
                       )""",ids)
    return len(ids)

                  OR display_label ~* '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}
    bad=[r["topic_id"] for r in rows if normalize_label(r["display_label"]) is None][:limit]
    if not bad:
        return 0
    return len(await db.fetch(
        """UPDATE aios.knowledge_topic SET status='retired',
                  vector_revision=vector_revision+1,graph_revision=graph_revision+1,
                  updated_at=now()
           WHERE topic_id=ANY($1::uuid[]) AND status='candidate'
           RETURNING topic_id""",bad))

                  OR display_label ~* '[[:space:]](and|or|of|the|with|for|to|in|at|a|an)
    bad=[r["topic_id"] for r in rows if normalize_label(r["display_label"]) is None][:limit]
    if not bad:
        return 0
    return len(await db.fetch(
        """UPDATE aios.knowledge_topic SET status='retired',
                  vector_revision=vector_revision+1,graph_revision=graph_revision+1,
                  updated_at=now()
           WHERE topic_id=ANY($1::uuid[]) AND status='candidate'
           RETURNING topic_id""",bad))
)
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
