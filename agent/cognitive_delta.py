from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aios_app.db import Database


@dataclass(frozen=True)
class CognitiveDelta:
    instance_id: UUID
    source_from_node_id: UUID | None
    source_through_node_id: UUID | None
    nodes: tuple[dict[str, Any], ...]
    scene_transitions: tuple[dict[str, Any], ...]

    def as_prompt_context(self) -> dict[str, Any]:
        return {
            "source_from_node_id": str(self.source_from_node_id) if self.source_from_node_id else None,
            "source_through_node_id": str(self.source_through_node_id) if self.source_through_node_id else None,
            "experience": list(self.nodes),
            "scene_changes": list(self.scene_transitions),
        }


class CognitiveDeltaService:
    """Resolve a bounded, branch-safe episode of experience for metacognition."""

    def __init__(self, db: Database):
        self.db = db

    async def build(
        self, *, instance_id: UUID, from_node_id: UUID | None,
        through_node_id: UUID | None, limit: int = 32,
    ) -> CognitiveDelta:
        if through_node_id is None:
            return CognitiveDelta(instance_id, from_node_id, through_node_id, (), ())
        rows = await self.db.fetch(
            """
            WITH RECURSIVE ancestry AS (
                SELECT dn.node_id, dn.event_id, dn.timeline_id, 0 AS depth
                FROM aios.dag_node dn WHERE dn.node_id=$2
                UNION ALL
                SELECT parent.node_id, parent.event_id, parent.timeline_id, a.depth + 1
                FROM ancestry a
                JOIN aios.dag_edge de ON de.child_node_id=a.node_id
                JOIN aios.dag_node parent ON parent.node_id=de.parent_node_id
                WHERE a.depth < $4 AND a.node_id IS DISTINCT FROM $3
            )
            SELECT a.node_id, a.event_id, ie.kind, ie.speaker_id, ie.speaker_role,
                   ie.recipient_id, ie.message_text, ie.event_time, a.depth
            FROM ancestry a
            LEFT JOIN aios.ingest_event ie ON ie.event_id=a.event_id
            WHERE a.node_id IS DISTINCT FROM $3
            ORDER BY a.depth DESC
            LIMIT $4
            """,
            instance_id, through_node_id, from_node_id, max(1, min(int(limit), 128)),
        )
        nodes = tuple({
            "node_id": str(r["node_id"]), "event_id": r["event_id"],
            "kind": r["kind"], "speaker_id": r["speaker_id"],
            "speaker_role": r["speaker_role"], "recipient_id": r["recipient_id"],
            "text": r["message_text"], "event_time": str(r["event_time"] or ""),
        } for r in rows)
        node_ids = [UUID(n["node_id"]) for n in nodes]
        transitions = ()
        if node_ids:
            trows = await self.db.fetch(
                """
                SELECT slot_key,before_value,after_value,source_node_id,runtime_node_id,created_at
                FROM aios.character_scene_transition
                WHERE instance_id=$1
                  AND (source_node_id = ANY($2::uuid[]) OR runtime_node_id = ANY($2::uuid[]))
                ORDER BY created_at, transition_id
                """,
                instance_id, node_ids,
            )
            transitions = tuple({
                "slot": r["slot_key"], "before": r["before_value"], "after": r["after_value"],
                "source_node_id": str(r["source_node_id"] or r["runtime_node_id"] or ""),
            } for r in trows)
        return CognitiveDelta(instance_id, from_node_id, through_node_id, nodes, transitions)
