from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from aios_app.db import Database


SAFE_DEFAULT = {
    "read_only": "allow",
    "internal_write": "allow",
    "external_reversible": "require_approval",
    "external_sensitive": "require_approval",
}


class ActionPolicyService:
    def __init__(self, db: Database):
        self.db = db

    async def disposition(
        self, instance_id: UUID, action_type: str, side_effect_class: str
    ) -> str:
        row = await self.db.fetchrow(
            """SELECT disposition FROM aios.character_action_policy
               WHERE instance_id=$1 AND action_type=$2""",
            instance_id, action_type,
        )
        if row:
            return str(row["disposition"])
        return SAFE_DEFAULT.get(side_effect_class, "require_approval")

    async def set_policy(
        self, instance_id: UUID, action_type: str, disposition: str, actor: str = "operator"
    ) -> None:
        if disposition not in {"allow","require_approval","deny"}:
            raise ValueError("invalid policy disposition")
        await self.db.execute(
            """INSERT INTO aios.character_action_policy(instance_id,action_type,disposition,updated_by)
               VALUES($1,$2,$3,$4) ON CONFLICT(instance_id,action_type) DO UPDATE SET
               disposition=EXCLUDED.disposition,updated_by=EXCLUDED.updated_by,updated_at=now()""",
            instance_id, action_type, disposition, actor,
        )

    async def request_approval(self, action_id: UUID) -> UUID:
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.character_action_approval(action_id)
               VALUES($1) ON CONFLICT(action_id) DO UPDATE SET action_id=EXCLUDED.action_id
               RETURNING approval_id""",
            action_id,
        )
        return row["approval_id"]

    async def decide(self, action_id: UUID, approved: bool, actor: str, reason: str | None = None) -> None:
        await self.db.execute(
            """UPDATE aios.character_action_approval SET status=$2,decided_at=now(),
               decided_by=$3,reason=$4 WHERE action_id=$1 AND status='pending'""",
            action_id, "approved" if approved else "denied", actor, reason,
        )

    async def is_approved(self, action_id: UUID) -> bool:
        row = await self.db.fetchrow(
            "SELECT status FROM aios.character_action_approval WHERE action_id=$1",
            action_id,
        )
        return bool(row and row["status"] == "approved")
