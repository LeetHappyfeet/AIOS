from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService


class SceneGoalReconciler:
    """Resolve explicit success/failure contracts against branch-local scene projections.

    Matching is deterministic; the projected outcome does not acquire verified
    learning authority merely because this matcher executed without inference.

    Free-text goals remain semantic and are left to planning.review.  This layer
    deliberately refuses to infer contracts from prose: deterministic lifecycle
    effects require structure supplied when the goal is created.
    """

    OPERATORS = frozenset({"equals", "not_equals", "contains"})

    def __init__(self, db: Database):
        self.db = db
        self.goals = CharacterGoalService(db)

    async def reconcile(
        self,
        *,
        instance_id: UUID,
        scene: Mapping[str, Any],
        source_node_id: UUID | None,
        snapshot_id: UUID | None = None,
    ) -> list[UUID]:
        if source_node_id is None:
            return []
        rows = await self.db.fetch(
            """SELECT g.goal_id,a.attempt_id,a.contract_snapshot
               FROM aios.character_agent_goal g
               JOIN aios.character_goal_attempt a ON a.goal_id=g.goal_id AND a.closed_at IS NULL
               JOIN aios.dag_node n ON n.node_id=$2 AND n.timeline_id=a.source_timeline_id
               WHERE g.instance_id=$1 AND g.status IN ('active','scheduled')""",
            instance_id, source_node_id,
        )
        resolved: list[UUID] = []
        for row in rows:
            contracts = self.goals._json_object(row["contract_snapshot"])
            success = contracts.get("success")
            failure = contracts.get("failure")
            success_match = isinstance(success, Mapping) and self._matches(scene, success)[0]
            failure_match = isinstance(failure, Mapping) and self._matches(scene, failure)[0]
            # Contradictory contracts require review; never let iteration order
            # decide a consequential outcome.
            if bool(success_match) == bool(failure_match):
                continue
            status = "completed" if success_match else "failed"
            contract = success if success_match else failure
            goal_id = row["goal_id"]
            evidence_id = f"scene:{snapshot_id or source_node_id}:{goal_id}:{status}"
            await self.db.execute(
                """INSERT INTO aios.character_goal_evidence(
                       goal_id,instance_id,evidence_type,relation,evidence_id,
                       source_node_id,confidence,meta)
                   VALUES($1,$2,'scene_contract',$3,$4,$5,1.0,$6::jsonb)
                   ON CONFLICT DO NOTHING""",
                goal_id, instance_id, "completion_candidate" if success_match else "contradiction",
                evidence_id, source_node_id,
                json.dumps({"contract": dict(contract), "observed": self._matches(scene, contract)[1],
                            "snapshot_id": str(snapshot_id) if snapshot_id else None}, default=str),
            )
            try:
                await self.goals.finish(
                    instance_id=instance_id, goal_id=goal_id, status=status,
                    resolution_kind="scene_contract_satisfied" if success_match else "scene_failure_contract_satisfied",
                    verification="projected", source_node_id=source_node_id,
                    evidence_ids=[evidence_id], attempt_id=row["attempt_id"], refresh_scene=False,
                )
            except LookupError:
                continue
            resolved.append(goal_id)
        return resolved

    @classmethod
    def _matches(
        cls, scene: Mapping[str, Any], contract: Mapping[str, Any]
    ) -> tuple[bool, Any]:
        slot = str(contract.get("slot") or "").strip()
        if not slot or slot not in scene or "value" not in contract:
            return False, None
        value: Any = scene.get(slot)
        path = str(contract.get("path") or "").strip()
        if path:
            for part in path.split("."):
                if not isinstance(value, Mapping) or part not in value:
                    return False, None
                value = value[part]
        operator = str(contract.get("operator") or "equals")
        if operator not in cls.OPERATORS:
            return False, value
        expected = contract.get("value")
        if operator == "equals":
            return type(value) is type(expected) and value == expected, value
        if operator == "not_equals":
            return type(value) is type(expected) and value != expected, value
        if isinstance(value, (list, tuple, set)):
            return expected in value, value
        if isinstance(value, Mapping):
            return expected in value or expected in value.values(), value
        if isinstance(value, str) and isinstance(expected, str) and expected:
            return expected in value, value
        return False, value
