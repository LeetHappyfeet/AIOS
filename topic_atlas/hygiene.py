"""Bounded cleanup of legacy non-linguistic Topic Atlas candidates.

Source mentions and discovery receipts remain available for audit. Retiring an
advisory subject does not invalidate its underlying proposition or observation.
"""
from __future__ import annotations

from .collector import normalize_label


async def retire_invalid_candidates_once(db, *, limit: int = 16) -> int:
    """Retire already-discovered invalid candidates without starving on valid rows.

    The SQL predicate narrows the historical search to obvious identifiers and
    trailing function words. Python's shared normalize_label policy is the
    final check. Registered/organized topics are never automatically retired.
    """
    budget = max(1, min(int(limit), 64))
    rows = await db.fetch(
        """SELECT topic_id, display_label
           FROM aios.knowledge_topic
           WHERE status = 'candidate'
             AND (
                 display_label ~* '^[a-f0-9]{32,64}$'
                 OR display_label ~* '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$'
                 OR display_label ~* '[[:space:]](and|or|of|the|with|for|to|in|at|a|an)$'
             )
           ORDER BY updated_at, topic_id
           LIMIT $1""",
        min(budget * 12, 512),
    )
    bad = [
        row["topic_id"] for row in rows
        if normalize_label(row["display_label"]) is None
    ][:budget]
    if not bad:
        return 0

    async with db.connection() as con:
        async with con.transaction():
            retired = await con.fetch(
                """UPDATE aios.knowledge_topic
                   SET status = 'retired',
                       vector_revision = vector_revision + 1,
                       graph_revision = graph_revision + 1,
                       updated_at = now()
                   WHERE topic_id = ANY($1::uuid[])
                     AND status = 'candidate'
                   RETURNING topic_id""",
                bad,
            )
            retired_ids = [row["topic_id"] for row in retired]
            if retired_ids:
                # A surviving topic's navigation graph must stop referring to
                # newly retired neighbors. Both revisions change atomically.
                await con.execute(
                    """UPDATE aios.knowledge_topic AS t
                       SET graph_revision = t.graph_revision + 1,
                           updated_at = now()
                       WHERE t.status <> 'retired'
                         AND t.topic_id IN (
                             SELECT r.target_topic_id
                             FROM aios.knowledge_topic_relation AS r
                             WHERE r.source_topic_id = ANY($1::uuid[])
                             UNION
                             SELECT r.source_topic_id
                             FROM aios.knowledge_topic_relation AS r
                             WHERE r.target_topic_id = ANY($1::uuid[])
                         )""",
                    retired_ids,
                )
    return len(retired_ids)
