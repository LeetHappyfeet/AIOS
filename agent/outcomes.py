"""Evidence-authorized outcomes. This service never assigns reward amounts."""
from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.epistemic.goals import CharacterGoalService


def mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


class OutcomeResolver:
    def __init__(self, db):
        self.db = db

    async def link_strategy(self, *, instance_id: UUID, goal_id: UUID,
                            strategy_key: str, receipt_type: str, receipt_id: UUID) -> None:
        """Host-only association with actually executed, goal-linked work.

        One explicit strategy per attempt in v1. No attribution to every action
        that merely preceded success. No model-facing action exposes this API.
        """
        if receipt_type not in {"action", "cognitive_operation"}:
            raise ValueError("invalid receipt type")
        if not strategy_key.strip() or len(strategy_key) > 120:
            raise ValueError("invalid strategy key")
        table = "character_action" if receipt_type == "action" else "character_cognitive_operation"
        key = "action_id" if receipt_type == "action" else "operation_id"
        payload = "arguments" if receipt_type == "action" else "input"
        async with self.db.connection() as con:
            async with con.transaction():
                await con.fetchrow("SELECT goal_id FROM aios.character_agent_goal WHERE goal_id=$1 AND instance_id=$2 FOR UPDATE",
                                   goal_id, instance_id)
                attempt = await con.fetchrow(
                    "SELECT * FROM aios.character_goal_attempt WHERE goal_id=$1 AND instance_id=$2 AND closed_at IS NULL",
                    goal_id, instance_id)
                if not attempt:
                    raise LookupError("open attempt not found")
                receipt = await con.fetchrow(
                    f"SELECT * FROM aios.{table} WHERE {key}=$1 AND instance_id=$2 AND {payload}->>'goal_id'=$3",
                    receipt_id, instance_id, str(goal_id))
                if (not receipt or receipt['status'] not in {'running', 'succeeded', 'failed'}
                        or receipt['created_at'] < attempt['started_at']):
                    raise ValueError("strategy requires executed work from this attempt")
                await con.execute(
                    """INSERT INTO aios.character_attempt_strategy(attempt_id,strategy_key,receipt_type,receipt_id)
                       VALUES($1,$2,$3,$4) ON CONFLICT(attempt_id) DO NOTHING""",
                    attempt['attempt_id'], strategy_key, receipt_type, receipt_id)
                linked = await con.fetchrow("SELECT * FROM aios.character_attempt_strategy WHERE attempt_id=$1",
                                            attempt['attempt_id'])
                if (linked['strategy_key'], linked['receipt_type'], linked['receipt_id']) != (strategy_key, receipt_type, receipt_id):
                    raise ValueError("attempt already has a different strategy association")

    async def resolve_receipt(self, *, instance_id: UUID, acquisition_id: UUID) -> UUID:
        """Admit an exact typed outcome receipt through the authority membrane.

        A generic canonical acquisition is insufficient. Its immutable source
        must explicitly identify the goal, attempt, and outcome on this timeline.
        """
        async with self.db.connection() as con:
            async with con.transaction():
                proof = await con.fetchrow(
                    """SELECT q.receipt,k.dag_node_id,n.timeline_id,k.created_at
                       FROM aios.knowledge_acquisition_event k
                       JOIN aios.character_outcome_receipt q USING(acquisition_id)
                       JOIN aios.epistemic_authority_admission e USING(acquisition_id)
                       JOIN aios.dag_node n ON n.node_id=k.dag_node_id
                       WHERE k.acquisition_id=$1 AND k.instance_id=$2
                         AND q.instance_id=k.instance_id AND q.source_node_id=k.dag_node_id
                         AND e.origin_kind='deterministic_tool' AND e.authority_state='canonical'
                         AND 'action_precondition'=ANY(e.authorized_uses)""",
                    acquisition_id, instance_id)
                if not proof:
                    raise PermissionError("outcome receipt lacks deterministic evidence authority")
                receipt = mapping(proof['receipt'])
                try:
                    goal_id, attempt_id = UUID(receipt['goal_id']), UUID(receipt['attempt_id'])
                    outcome = receipt['outcome']
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValueError("invalid typed outcome receipt") from exc
                if outcome not in {'success', 'failure'}:
                    raise ValueError("receipt must establish success or failure")
                goal = await con.fetchrow("SELECT * FROM aios.character_agent_goal WHERE goal_id=$1 AND instance_id=$2 FOR UPDATE",
                                          goal_id, instance_id)
                attempt = await con.fetchrow(
                    "SELECT * FROM aios.character_goal_attempt WHERE attempt_id=$1 AND goal_id=$2 AND instance_id=$3",
                    attempt_id, goal_id, instance_id)
                if not goal or not attempt or attempt['source_timeline_id'] != proof['timeline_id']:
                    raise ValueError("receipt does not belong to this attempt timeline")
                if proof['created_at'] < attempt['started_at']:
                    raise ValueError("receipt predates attempt")
                # A delayed receipt for an old attempt may correct its history,
                # but never mutate the new attempt or its goal lifecycle.
                if attempt['closed_at'] is None:
                    await CharacterGoalService(con).finish(
                        instance_id=instance_id, goal_id=goal_id,
                        status='completed' if outcome == 'success' else 'failed',
                        resolution_kind='typed_outcome_receipt', source_node_id=proof['dag_node_id'],
                        evidence_ids=[str(acquisition_id)], attempt_id=attempt_id, refresh_scene=False)
                previous = await con.fetchrow(
                    """SELECT o.* FROM aios.character_outcome_event o WHERE o.attempt_id=$1
                       AND NOT EXISTS(SELECT 1 FROM aios.character_outcome_event c WHERE c.supersedes_outcome_id=o.outcome_id)
                       FOR UPDATE""", attempt_id)
                if not previous:
                    raise LookupError("attempt outcome not found")
                dedupe = f"typed-receipt:{acquisition_id}"
                existing = await con.fetchrow("SELECT outcome_id FROM aios.character_outcome_event WHERE instance_id=$1 AND dedupe_key=$2",
                                              instance_id, dedupe)
                if existing:
                    return existing['outcome_id']
                return await self._append_correction(
                    con, previous, dedupe_key=dedupe, outcome_type=outcome,
                    verification='verified', resolution_kind='typed_outcome_receipt',
                    source_node_id=proof['dag_node_id'], evidence_ids=[str(acquisition_id)],
                    attribution=receipt.get('attribution', 'unknown'),
                    cause_class=receipt.get('cause_class', 'unknown'),
                    credit_receipt_id=receipt.get('strategy_receipt_id'))

    async def process_receipts(self, *, limit: int = 16) -> int:
        """Bounded supervisor intake; invalid receipts are audited once."""
        rows = await self.db.fetch(
            "SELECT acquisition_id,instance_id FROM aios.character_outcome_receipt WHERE status='pending' ORDER BY created_at LIMIT $1",
            max(1, min(limit, 100)))
        for row in rows:
            try:
                await self.resolve_receipt(instance_id=row['instance_id'], acquisition_id=row['acquisition_id'])
                status, reason = 'accepted', None
            except (PermissionError, ValueError, LookupError) as exc:
                status, reason = 'rejected', str(exc)[:500]
            # Unexpected database errors leave pending work retryable.
            await self.db.execute(
                "UPDATE aios.character_outcome_receipt SET status=$2,reason=$3,resolved_at=now() WHERE acquisition_id=$1 AND status='pending'",
                row['acquisition_id'], status, reason)
        return len(rows)

    async def invalidate(self, *, instance_id: UUID, outcome_id: UUID,
                         request_id: UUID, reason: str) -> UUID:
        """Append an unverified correction; shadow projection drops old credit."""
        if not reason.strip():
            raise ValueError("correction requires a reason")
        async with self.db.connection() as con:
            async with con.transaction():
                existing = await con.fetchrow("SELECT outcome_id FROM aios.character_outcome_event WHERE instance_id=$1 AND dedupe_key=$2",
                                              instance_id, f"correction:{request_id}")
                if existing:
                    return existing['outcome_id']
                old = await con.fetchrow("SELECT * FROM aios.character_outcome_event WHERE outcome_id=$1 AND instance_id=$2 FOR UPDATE",
                                         outcome_id, instance_id)
                if not old:
                    raise LookupError("outcome not found")
                if await con.fetchval("SELECT 1 FROM aios.character_outcome_event WHERE supersedes_outcome_id=$1", outcome_id):
                    raise ValueError("outcome already superseded; correct its current successor")
                return await self._append_correction(con, old, dedupe_key=f"correction:{request_id}",
                    outcome_type='unresolved', verification='unverified', resolution_kind=reason[:500])

    @staticmethod
    async def _append_correction(con, old, *, dedupe_key, outcome_type, verification,
                                 resolution_kind, source_node_id=None, evidence_ids=(),
                                 attribution='unknown', cause_class='unknown', credit_receipt_id=None) -> UUID:
        if attribution not in {'unknown', 'self', 'external', 'mixed'}:
            raise ValueError("invalid attribution")
        if cause_class not in {'unknown', 'execution_failure', 'external_obstruction', 'achieved'}:
            raise ValueError("invalid cause class")
        if credit_receipt_id is not None:
            credit_receipt_id = str(UUID(str(credit_receipt_id)))
        row = await con.fetchrow(
            """INSERT INTO aios.character_outcome_event(instance_id,subject_id,attempt_id,outcome_type,
               verification,cause_class,attribution,source_node_id,source_timeline_id,evidence_ids,
               resolution_kind,supersedes_outcome_id,dedupe_key,meta)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13,$14::jsonb) RETURNING outcome_id""",
            old['instance_id'], old['subject_id'], old['attempt_id'], outcome_type, verification,
            cause_class, attribution, source_node_id, old['source_timeline_id'],
            json.dumps(list(evidence_ids)), resolution_kind, old['outcome_id'], dedupe_key,
            json.dumps({'credit_receipt_id': credit_receipt_id}))
        await con.execute("INSERT INTO aios.character_appraisal_pending(outcome_id) VALUES($1)", row['outcome_id'])
        return row['outcome_id']
