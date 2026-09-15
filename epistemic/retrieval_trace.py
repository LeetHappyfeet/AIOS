from __future__ import annotations

from typing import Any, Iterable

from aios_app.hud.context import HUDContext


async def record_world_retrieval(
    db: Any,
    context: HUDContext,
    items: Iterable[dict[str, Any]],
) -> str | None:
    """Record world propositions that survived retrieval selection.

    Recording retrieval is not knowledge acquisition.  The trace is evidence for
    later reconciliation with a character-generated turn.
    """
    selected = [item for item in items if item.get("proposition_id")]
    if not selected:
        return None

    trace_id = await db.fetchval(
        """
        INSERT INTO aios.retrieval_trace(
            instance_id, character_id, timeline_id, head_node_id
        ) VALUES ($1,$2,$3,$4)
        RETURNING trace_id
        """,
        context.instance_id,
        context.character_id,
        context.timeline_id,
        context.head_node_id,
    )

    for rank, item in enumerate(selected):
        relevance = item.get("relevance") or {}
        await db.execute(
            """
            INSERT INTO aios.retrieval_trace_item(
                trace_id, proposition_id, source_scope_key, retrieval_reason,
                rank, relevance_score, injected
            ) VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT DO NOTHING
            """,
            trace_id,
            item["proposition_id"],
            item.get("retrieval_scope") or "world",
            item.get("retrieval_reason") or "world_semantic",
            rank,
            relevance.get("total"),
            bool(item.get("injected", False)),
        )
        await db.execute(
            """
            INSERT INTO aios.character_retrieval_affinity(
                character_id, proposition_id, hit_count, affinity,
                last_retrieved_at
            ) VALUES ($1,$2,1,0.0,now())
            ON CONFLICT (character_id, proposition_id) DO UPDATE
            SET hit_count=aios.character_retrieval_affinity.hit_count + 1,
                last_retrieved_at=now(),
                updated_at=now()
            """,
            context.character_id,
            item["proposition_id"],
        )
    return str(trace_id)
