from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from aios_app.db import Database


PROJECTION_VERSION = "character-scene-v1"


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _slot_view(scene: Mapping[str, Any]) -> dict[str, Any]:
    """Stable top-level slots whose changes are useful to audit."""
    return {
        key: _jsonable(scene.get(key))
        for key in (
            "location",
            "present_entities",
            "relevant_objects",
            "immediate_goal",
            "pending_action",
            "last_significant_change",
        )
    }


class CharacterSceneStateStore:
    """Persist character-relative scene projections without becoming truth.

    Scene ownership is strictly character_instance.  We intentionally never
    search cognitive instance lineage here: a fork may seed from its parent,
    but sibling effective scenes are never unioned.
    """

    def __init__(self, db: Database):
        self.db = db

    async def materialize(
        self,
        *,
        instance_id: UUID,
        runtime_timeline_id: UUID,
        runtime_head_node_id: Optional[UUID],
        source_timeline_id: Optional[UUID],
        source_head_node_id: Optional[UUID],
        scene: Mapping[str, Any],
        evidence_node_ids: Sequence[UUID] = (),
    ) -> dict[str, Any]:
        normalized = _slot_view(scene)
        # Parent selection follows actual DAG ancestry, never "latest scene".
        # That distinction is what keeps swipes/alternatives and concurrent
        # timelines from borrowing state from an abandoned or sibling branch.
        previous = await self.db.fetchrow(
            """
            SELECT s.snapshot_id, s.scene_state
            FROM aios.character_scene_snapshot s
            WHERE s.instance_id=$1
              AND s.projection_version=$2
              AND (
                    (
                        $6::uuid IS NOT NULL
                        AND s.source_timeline_id IS NOT DISTINCT FROM $5
                        AND s.source_head_node_id IN (
                            SELECT de.parent_node_id
                            FROM aios.dag_edge de
                            WHERE de.timeline_id=$5
                              AND de.child_node_id=$6
                        )
                    )
                    OR
                    (
                        $4::uuid IS NOT NULL
                        AND s.runtime_timeline_id=$3
                        AND s.runtime_head_node_id IN (
                            SELECT de.parent_node_id
                            FROM aios.dag_edge de
                            WHERE de.timeline_id=$3
                              AND de.child_node_id=$4
                        )
                    )
              )
            ORDER BY
                CASE WHEN s.source_head_node_id IS NOT NULL THEN 0 ELSE 1 END,
                s.updated_at DESC
            LIMIT 1
            """,
            instance_id,
            PROJECTION_VERSION,
            runtime_timeline_id,
            runtime_head_node_id,
            source_timeline_id,
            source_head_node_id,
        )
        parent_snapshot_id = previous["snapshot_id"] if previous else None
        before = dict(previous["scene_state"] or {}) if previous else {}

        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.character_scene_snapshot (
                instance_id, runtime_timeline_id, runtime_head_node_id,
                source_timeline_id, source_head_node_id, parent_snapshot_id,
                scene_state, evidence_node_ids, projection_version
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::uuid[],$9)
            ON CONFLICT (
                instance_id, runtime_timeline_id, runtime_head_node_id,
                source_timeline_id, source_head_node_id, projection_version
            ) DO UPDATE
            SET scene_state=EXCLUDED.scene_state,
                evidence_node_ids=EXCLUDED.evidence_node_ids,
                updated_at=now()
            RETURNING snapshot_id, scene_state, projection_version
            """,
            instance_id,
            runtime_timeline_id,
            runtime_head_node_id,
            source_timeline_id,
            source_head_node_id,
            parent_snapshot_id,
            json.dumps(normalized, default=str),
            list(dict.fromkeys(evidence_node_ids)),
            PROJECTION_VERSION,
        )
        snapshot_id = row["snapshot_id"]

        # Re-materialization at the same coordinates is allowed while the live
        # semantic pipeline catches up. Replace audit deltas for that snapshot
        # instead of accumulating contradictory transitions.
        await self.db.execute(
            "DELETE FROM aios.character_scene_transition WHERE snapshot_id=$1",
            snapshot_id,
        )
        evidence_node_id = source_head_node_id or runtime_head_node_id
        for slot, after_value in normalized.items():
            before_value = before.get(slot)
            if before_value == after_value:
                continue
            persistence = "turn" if slot == "last_significant_change" else "until_changed"
            await self.db.execute(
                """
                INSERT INTO aios.character_scene_transition (
                    instance_id, snapshot_id, runtime_timeline_id, runtime_node_id,
                    source_timeline_id, source_node_id, slot_key,
                    before_value, after_value, persistence, confidence,
                    evidence_node_id, meta
                )
                VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,1.0,$11,
                    jsonb_build_object('projection_version',$12)
                )
                """,
                instance_id,
                snapshot_id,
                runtime_timeline_id,
                runtime_head_node_id,
                source_timeline_id,
                source_head_node_id,
                slot,
                json.dumps(_jsonable(before_value), default=str),
                json.dumps(_jsonable(after_value), default=str),
                persistence,
                evidence_node_id,
                PROJECTION_VERSION,
            )

        return {
            "snapshot_id": snapshot_id,
            "projection_version": row["projection_version"],
            **normalized,
        }

    async def seed_fork(
        self,
        *,
        parent_instance_id: UUID,
        child_instance_id: UUID,
        child_runtime_timeline_id: UUID,
        child_runtime_head_node_id: Optional[UUID],
        child_source_timeline_id: Optional[UUID],
        child_source_head_node_id: Optional[UUID],
    ) -> Optional[dict[str, Any]]:
        """Seed a child once from its parent's latest scene; never link siblings."""
        existing = await self.db.fetchrow(
            "SELECT snapshot_id FROM aios.character_scene_snapshot WHERE instance_id=$1 LIMIT 1",
            child_instance_id,
        )
        if existing:
            return None
        parent = await self.db.fetchrow(
            """
            SELECT scene_state, evidence_node_ids
            FROM aios.character_scene_snapshot
            WHERE instance_id=$1 AND projection_version=$2
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            parent_instance_id,
            PROJECTION_VERSION,
        )
        if not parent:
            return None
        return await self.materialize(
            instance_id=child_instance_id,
            runtime_timeline_id=child_runtime_timeline_id,
            runtime_head_node_id=child_runtime_head_node_id,
            source_timeline_id=child_source_timeline_id,
            source_head_node_id=child_source_head_node_id,
            scene=dict(parent["scene_state"] or {}),
            evidence_node_ids=tuple(parent["evidence_node_ids"] or ()),
        )
