"""Versioned shadow appraisal. No executive or scheduler reads these signals."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .outcomes import mapping

POLICY_VERSION = 'reinforcement-shadow-v1'


def validate_expectation(value: Mapping[str, Any]) -> dict[str, float]:
    result = {}
    for key in ('success_probability', 'importance'):
        if key not in value:
            continue
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError(f'{key} must be a finite number between zero and one')
        result[key] = float(number)
    if set(value) - {'success_probability', 'importance'}:
        raise ValueError('unknown expectation field')
    return result


@dataclass(frozen=True)
class Appraisal:
    target_type: str
    target_key: str
    signal_type: str
    magnitude: float
    reason: str


def appraise(outcome: Mapping[str, Any], expectation: Mapping[str, Any],
             strategy_key: str | None, *, receipt_terminal: bool = False) -> Appraisal:
    """Credit only a verified, explicitly self-attributed executed strategy."""
    neutral = lambda reason: Appraisal('observation', str(outcome['attempt_id']), 'no_credit', 0.0, reason)
    if outcome['verification'] != 'verified':
        return neutral('outcome has no verified authority')
    if outcome['outcome_type'] not in {'success', 'failure'}:
        return neutral('withdrawal or unresolved outcome is not a penalty')
    if outcome['attribution'] != 'self' or outcome['cause_class'] == 'external_obstruction':
        return neutral('strategy attribution is unknown, mixed, or external')
    if not strategy_key or not receipt_terminal:
        return neutral('no explicit executed strategy receipt')
    try:
        expected = validate_expectation(expectation)
    except ValueError:
        return neutral('invalid expectation snapshot')
    importance = expected.get('importance', 0.5)
    success = outcome['outcome_type'] == 'success'
    if 'success_probability' in expected:
        delta = float(success) - expected['success_probability']
        signal_type = 'prediction_error'
        reason = 'prediction error against expectation captured at attempt admission'
    else:
        delta = 1.0 if success else -1.0
        signal_type = 'outcome_feedback'
        reason = 'outcome feedback; no prior expectation, so surprise is unknown'
    return Appraisal('strategy', strategy_key, signal_type, round(0.25 * importance * delta, 8), reason)


class ShadowReinforcementService:
    def __init__(self, db):
        self.db = db

    async def process_pending(self, *, limit: int = 32) -> int:
        processed = 0
        # One small transaction per outcome. SKIP LOCKED and idempotent events
        # make concurrent supervisors and crash retries safe.
        for _ in range(max(1, min(limit, 100))):
            async with self.db.connection() as con:
                async with con.transaction():
                    row = await con.fetchrow(
                        """SELECT o.*,a.expectation_snapshot,s.strategy_key,
                           (o.meta->>'credit_receipt_id'=s.receipt_id::text) AND CASE s.receipt_type
                             WHEN 'action' THEN EXISTS(SELECT 1 FROM aios.character_action x WHERE x.action_id=s.receipt_id
                               AND x.instance_id=o.instance_id AND x.status IN ('succeeded','failed'))
                             WHEN 'cognitive_operation' THEN EXISTS(SELECT 1 FROM aios.character_cognitive_operation x
                               WHERE x.operation_id=s.receipt_id AND x.instance_id=o.instance_id AND x.status IN ('succeeded','failed'))
                             ELSE false END AS receipt_terminal
                           FROM aios.character_appraisal_pending p
                           JOIN aios.character_outcome_event o USING(outcome_id)
                           JOIN aios.character_goal_attempt a USING(attempt_id)
                           LEFT JOIN aios.character_attempt_strategy s USING(attempt_id)
                           ORDER BY p.created_at,p.outcome_id LIMIT 1 FOR UPDATE OF p SKIP LOCKED""")
                    if not row:
                        return processed
                    # Serialize projection rebuilds before taking a READ COMMITTED
                    # snapshot, so another committed event cannot be overwritten.
                    await con.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                                      f"shadow-appraisal:{row['instance_id']}")
                    signal = appraise(row, mapping(row['expectation_snapshot']), row['strategy_key'],
                                      receipt_terminal=row['receipt_terminal'])
                    await con.execute(
                        """INSERT INTO aios.character_reinforcement_event(outcome_id,target_type,target_key,signal_type,
                           magnitude,policy_version,reason) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING""",
                        row['outcome_id'], signal.target_type, signal.target_key,
                        signal.signal_type, signal.magnitude, POLICY_VERSION, signal.reason)
                    await self._rebuild(con, row['instance_id'])
                    await con.execute("DELETE FROM aios.character_appraisal_pending WHERE outcome_id=$1", row['outcome_id'])
                    processed += 1
        return processed

    async def rebuild(self, instance_id) -> None:
        async with self.db.connection() as con:
            async with con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"shadow-appraisal:{instance_id}")
                await self._rebuild(con, instance_id)

    @staticmethod
    async def _rebuild(con, instance_id):
        await con.execute("DELETE FROM aios.character_behavior_projection WHERE instance_id=$1 AND policy_version=$2",
                          instance_id, POLICY_VERSION)
        await con.execute(
            """INSERT INTO aios.character_behavior_projection(instance_id,scope_key,strategy_key,policy_version,
               success_count,failure_count,signal_mean)
               SELECT o.instance_id,COALESCE(o.source_timeline_id::text,'unscoped'),r.target_key,r.policy_version,
                 count(*) FILTER(WHERE o.outcome_type='success'),count(*) FILTER(WHERE o.outcome_type='failure'),avg(r.magnitude)
               FROM aios.character_reinforcement_event r JOIN aios.character_outcome_event o USING(outcome_id)
               WHERE o.instance_id=$1 AND r.policy_version=$2 AND r.target_type='strategy'
                 AND NOT EXISTS(SELECT 1 FROM aios.character_outcome_event c WHERE c.supersedes_outcome_id=o.outcome_id)
               GROUP BY o.instance_id,o.source_timeline_id,r.target_key,r.policy_version""", instance_id, POLICY_VERSION)

    async def inspect(self, instance_id, *, limit: int = 50) -> dict[str, Any]:
        outcomes = await self.db.fetch(
            """SELECT o.*,NOT EXISTS(SELECT 1 FROM aios.character_outcome_event c
               WHERE c.supersedes_outcome_id=o.outcome_id) AS effective
               FROM aios.character_outcome_event o WHERE instance_id=$1
               ORDER BY created_at DESC,outcome_id LIMIT $2""", instance_id, max(1, min(limit, 100)))
        signals = await self.db.fetch(
            """SELECT r.* FROM aios.character_reinforcement_event r JOIN aios.character_outcome_event o USING(outcome_id)
               WHERE o.instance_id=$1 ORDER BY r.created_at DESC LIMIT $2""", instance_id, max(1, min(limit, 100)))
        projections = await self.db.fetch("SELECT * FROM aios.character_behavior_projection WHERE instance_id=$1 AND policy_version=$2",
                                          instance_id, POLICY_VERSION)
        pending_row = await self.db.fetchrow("SELECT count(*) FROM aios.character_appraisal_pending p JOIN aios.character_outcome_event o USING(outcome_id) WHERE o.instance_id=$1", instance_id)
        attempts = await self.db.fetch("SELECT * FROM aios.character_goal_attempt WHERE instance_id=$1 ORDER BY started_at DESC LIMIT $2",
                                      instance_id, max(1, min(limit, 100)))
        return {'shadow': True, 'policy_version': POLICY_VERSION, 'pending': pending_row[0],
                'attempts': [dict(x) for x in attempts], 'outcomes': [dict(x) for x in outcomes],
                'signals': [dict(x) for x in signals], 'projections': [dict(x) for x in projections]}
