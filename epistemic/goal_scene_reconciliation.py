from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService


class SceneGoalReconciler:
    """Close goals only when an explicit scene contract is objectively satisfied.

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
        rows = await self.db.fetch(
            """SELECT goal_id,meta FROM aios.character_agent_goal
               WHERE instance_id=$1 AND status='active'
                 AND meta ? 'completion_contract'""",
            instance_id,
        )
        completed: list[UUID] = []
        for row in rows:
            meta = self.goals._json_object(row["meta"])
            contract = meta.get("completion_contract")
            if not isinstance(contract, Mapping):
                continue
            matched, observed = self._matches(scene, contract)
            if not matched:
                continue
            goal_id = row["goal_id"]
            evidence_id = (
                f"scene:{snapshot_id}:{goal_id}" if snapshot_id
                else f"scene:{source_node_id}:{goal_id}"
            )
            await self.db.execute(
                """INSERT INTO aios.character_goal_evidence(
                       goal_id,instance_id,evidence_type,relation,evidence_id,
                       source_node_id,confidence,meta)
                   VALUES($1,$2,'scene_contract','completion_candidate',$3,$4,1.0,$5::jsonb)
                   ON CONFLICT DO NOTHING""",
                goal_id, instance_id, evidence_id, source_node_id,
                json.dumps({
                    "completion_contract": dict(contract),
                    "observed": observed,
                    "snapshot_id": str(snapshot_id) if snapshot_id else None,
                }, default=str),
            )
            try:
                await self.goals.finish(
                    instance_id=instance_id,
                    goal_id=goal_id,
                    status="completed",
                    refresh_scene=False,
                )
            except LookupError:
                continue
            await self.db.execute(
                """UPDATE aios.character_agent_goal
                   SET meta=meta || $3::jsonb,updated_at=now()
                   WHERE goal_id=$1 AND instance_id=$2""",
                goal_id, instance_id,
                json.dumps({
                    "resolution_kind": "scene_contract_satisfied",
                    "completion_confidence": 1.0,
                }),
            )
            completed.append(goal_id)
        return completed

    @classmethod
    def _matches(
        cls, scene: Mapping[str, Any], contract: Mapping[str, Any]
    ) -> tuple[bool, Any]:
        slot = str(contract.get("slot") or "").strip()
        if not slot:
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
            return value == expected, value
        if operator == "not_equals":
            return value != expected, value
        if isinstance(value, (list, tuple, set)):
            return expected in value, value
        if isinstance(value, Mapping):
            return expected in value or expected in value.values(), value
        return str(expected) in str(value), value
