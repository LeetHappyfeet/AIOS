from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from aios_app.db import Database

from .projection import project_committed_world_state
from .registry import CausalDomainRegistry
from .rules import validate_action_rules
from .types import (
    AdmissionDecision,
    CausalCandidate,
    CausalEvaluation,
    CausalState,
)


class CausalConflict(RuntimeError):
    """Raised when an optimistic causal state version is stale."""


class CausalRejected(ValueError):
    """Raised by require_commit() when a candidate cannot enter /world."""

    def __init__(self, evaluation: CausalEvaluation):
        super().__init__(f"{evaluation.decision.value}: {evaluation.reason}")
        self.evaluation = evaluation


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class CausalIntegrityKernel:
    """Authoritative branch-consistency boundary for objective AIOS state.

    The kernel owns no epistemic policy.  It never reads or writes character
    knowledge.  It serializes deterministic transitions per
    (world,timeline,domain,entity,state_key), records the decision, and only then
    projects committed reality into objective /world structures.
    """

    def __init__(
        self,
        db: Database,
        *,
        registry: Optional[CausalDomainRegistry] = None,
    ) -> None:
        self.db = db
        self.registry = registry or CausalDomainRegistry()

    async def evaluate(self, candidate: CausalCandidate) -> CausalEvaluation:
        async with self.db.connection() as con:
            state = await self._load_state(con, candidate, for_update=False)
            rules = await self._load_domain_rules(con, candidate.world_id, candidate.domain_id)
            return self.registry.get(candidate.domain_id).evaluate(
                state,
                candidate,
                rules=rules,
            )

    async def commit(
        self,
        candidate: CausalCandidate,
        *,
        expected_state_version: Optional[int] = None,
    ) -> dict[str, Any]:
        """Evaluate and atomically commit one candidate if causally admissible."""

        async with self.db.connection() as con:
            async with con.transaction():
                lock_key = (
                    f"causal::{candidate.world_id}::{candidate.timeline_id}::"
                    f"{candidate.domain_id}::{candidate.entity_id}::{candidate.state_key}"
                )
                await con.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    lock_key,
                )

                candidate_id = await self._persist_candidate(con, candidate)
                candidate = replace(candidate, candidate_id=candidate_id)
                state = await self._load_state(con, candidate, for_update=True)

                if (
                    expected_state_version is not None
                    and (state.version if state else 0) != expected_state_version
                ):
                    raise CausalConflict(
                        f"stale causal state_version {expected_state_version}; "
                        f"current is {state.version if state else 0}"
                    )

                rules = await self._load_domain_rules(
                    con,
                    candidate.world_id,
                    candidate.domain_id,
                )
                evaluation = self.registry.get(candidate.domain_id).evaluate(
                    state,
                    candidate,
                    rules=rules,
                )

                before_version = state.version if state else 0
                after_version = before_version
                world_event_id: Optional[UUID] = None

                if evaluation.committable:
                    after_version = before_version + 1
                    world_event_id = await self._append_world_event(
                        con,
                        candidate=candidate,
                        evaluation=evaluation,
                        before_version=before_version,
                        after_version=after_version,
                    )
                    await self._write_state(
                        con,
                        candidate=candidate,
                        evaluation=evaluation,
                        world_event_id=world_event_id,
                        state_version=after_version,
                    )
                    await project_committed_world_state(
                        con,
                        world_id=candidate.world_id,
                        timeline_id=candidate.timeline_id,
                        domain_id=candidate.domain_id,
                        entity_id=candidate.entity_id,
                        state_key=candidate.state_key,
                        value=evaluation.after,
                        dag_node_id=candidate.dag_node_id,
                    )

                admission_id = await self._record_admission(
                    con,
                    candidate_id=candidate_id,
                    evaluation=evaluation,
                    before_version=before_version,
                    after_version=after_version,
                )

                return {
                    "candidate_id": candidate_id,
                    "admission_id": admission_id,
                    "decision": evaluation.decision.value,
                    "reason_code": evaluation.reason_code,
                    "reason": evaluation.reason,
                    "state_version_before": before_version,
                    "state_version_after": after_version,
                    "world_event_id": world_event_id,
                    "committed": evaluation.committable,
                    "before": evaluation.before,
                    "after": evaluation.after,
                    "delta": evaluation.delta,
                    "latent_events": list(evaluation.latent_events),
                }

    async def require_commit(
        self,
        candidate: CausalCandidate,
        *,
        expected_state_version: Optional[int] = None,
    ) -> dict[str, Any]:
        result = await self.commit(
            candidate,
            expected_state_version=expected_state_version,
        )
        if not result["committed"]:
            raise CausalRejected(
                CausalEvaluation(
                    decision=AdmissionDecision(result["decision"]),
                    reason_code=result["reason_code"],
                    reason=result["reason"],
                    before=result.get("before"),
                    after=result.get("after"),
                    delta=result.get("delta"),
                    latent_events=tuple(result.get("latent_events") or ()),
                )
            )
        return result

    async def validate_runtime_action(
        self,
        *,
        world_id: UUID,
        action_type: str,
        actor_entity_type: Optional[str],
        target_entity_type: Optional[str],
        has_target: bool,
    ) -> None:
        """Central home for the existing deterministic world_rule contract."""
        rows = await self.db.fetch(
            """
            SELECT rule_key, rule_type, rule_data
            FROM aios.world_rule
            WHERE world_id=$1 AND enabled=true
              AND (rule_data->>'domain_id' IS NULL OR rule_data->>'domain_id' IN ('runtime.action','*'))
            ORDER BY priority, rule_key
            """,
            world_id,
        )
        validate_action_rules(
            [dict(row) for row in rows],
            action_type=action_type,
            actor_entity_type=actor_entity_type,
            target_entity_type=target_entity_type,
            has_target=has_target,
        )

    async def state(
        self,
        *,
        world_id: UUID,
        timeline_id: UUID,
        domain_id: str,
        entity_id: UUID,
        state_key: str,
    ) -> CausalState | None:
        candidate = CausalCandidate(
            world_id=world_id,
            timeline_id=timeline_id,
            domain_id=domain_id,
            event_type="read",
            entity_id=entity_id,
            state_key=state_key,
            value=None,
        )
        async with self.db.connection() as con:
            return await self._load_state(con, candidate, for_update=False)

    async def fork_state(
        self,
        *,
        source_world_id: UUID,
        source_timeline_id: UUID,
        target_world_id: UUID,
        target_timeline_id: UUID,
        through_node_id: Optional[UUID] = None,
    ) -> int:
        """Copy materialized deterministic state into a new branch.

        The copied rows are a branch snapshot, not character knowledge.  Each
        target row starts a new local version sequence while retaining source
        coordinates in metadata for audit.
        """
        async with self.db.connection() as con:
            async with con.transaction():
                rows = await con.fetch(
                    """
                    SELECT domain_id, entity_id, state_key, value_json,
                           state_version, occurred_at
                    FROM aios.causal_state
                    WHERE world_id=$1 AND timeline_id=$2
                    """,
                    source_world_id,
                    source_timeline_id,
                )
                copied = 0
                for row in rows:
                    entity_exists = await con.fetchrow(
                        "SELECT 1 FROM aios.world_entity WHERE world_id=$1 AND entity_id=$2",
                        target_world_id,
                        row["entity_id"],
                    )
                    if not entity_exists:
                        # Character/runtime forks usually allocate new entity IDs;
                        # callers should seed those entity-specific states after
                        # mapping.  Shared world entities can be copied directly.
                        continue
                    await con.execute(
                        """
                        INSERT INTO aios.causal_state (
                            world_id,timeline_id,domain_id,entity_id,state_key,
                            value_json,state_version,last_node_id,occurred_at,meta
                        )
                        VALUES ($1,$2,$3,$4,$5,$6::jsonb,1,$7,$8,
                                jsonb_build_object(
                                    'forked_from_world_id',$9::text,
                                    'forked_from_timeline_id',$10::text,
                                    'source_state_version',$11::bigint
                                ))
                        ON CONFLICT (world_id,timeline_id,domain_id,entity_id,state_key)
                        DO NOTHING
                        """,
                        target_world_id,
                        target_timeline_id,
                        row["domain_id"],
                        row["entity_id"],
                        row["state_key"],
                        json.dumps(_jsonable(_decode_json(row["value_json"]))),
                        through_node_id,
                        row["occurred_at"],
                        source_world_id,
                        source_timeline_id,
                        row["state_version"],
                    )
                    copied += 1
                return copied

    async def _load_state(self, con: Any, candidate: CausalCandidate, *, for_update: bool) -> CausalState | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = await con.fetchrow(
            """
            SELECT value_json, state_version, last_event_id, last_node_id, occurred_at
            FROM aios.causal_state
            WHERE world_id=$1 AND timeline_id=$2 AND domain_id=$3
              AND entity_id=$4 AND state_key=$5
            """ + suffix,
            candidate.world_id,
            candidate.timeline_id,
            candidate.domain_id,
            candidate.entity_id,
            candidate.state_key,
        )
        if not row:
            return None
        return CausalState(
            value=_decode_json(row["value_json"]),
            version=int(row["state_version"]),
            last_event_id=row["last_event_id"],
            last_node_id=row["last_node_id"],
            occurred_at=row["occurred_at"],
        )

    async def _load_domain_rules(self, con: Any, world_id: UUID, domain_id: str) -> list[dict]:
        rows = await con.fetch(
            """
            SELECT rule_key, rule_type, rule_data
            FROM aios.world_rule
            WHERE world_id=$1 AND enabled=true
              AND (rule_data->>'domain_id'=$2 OR rule_data->>'domain_id'='*')
            ORDER BY priority, rule_key
            """,
            world_id,
            domain_id,
        )
        return [dict(row) for row in rows]

    async def _persist_candidate(self, con: Any, candidate: CausalCandidate) -> UUID:
        if candidate.candidate_id:
            row = await con.fetchrow(
                "SELECT candidate_id FROM aios.causal_candidate WHERE candidate_id=$1",
                candidate.candidate_id,
            )
            if row:
                return row["candidate_id"]

        row = await con.fetchrow(
            """
            INSERT INTO aios.causal_candidate (
                candidate_id, world_id, timeline_id, dag_node_id, domain_id,
                event_type, entity_id, target_entity_id, state_key, value_json,
                parameters, source_kind, source_ref, authority_kind, occurred_at,
                claim_id, frame_id, proposition_id
            )
            VALUES (
                COALESCE($1, gen_random_uuid()),$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,
                $11::jsonb,$12,$13,$14,$15,$16,$17,$18
            )
            RETURNING candidate_id
            """,
            candidate.candidate_id,
            candidate.world_id,
            candidate.timeline_id,
            candidate.dag_node_id,
            candidate.domain_id,
            candidate.event_type,
            candidate.entity_id,
            candidate.target_entity_id,
            candidate.state_key,
            json.dumps(_jsonable(candidate.value)),
            json.dumps(_jsonable(candidate.parameters)),
            candidate.source_kind,
            candidate.source_ref,
            candidate.authority_kind,
            candidate.occurred_at,
            candidate.claim_id,
            candidate.frame_id,
            candidate.proposition_id,
        )
        return row["candidate_id"]

    async def _append_world_event(
        self,
        con: Any,
        *,
        candidate: CausalCandidate,
        evaluation: CausalEvaluation,
        before_version: int,
        after_version: int,
    ) -> UUID:
        row = await con.fetchrow(
            """
            INSERT INTO aios.world_event (
                world_id, timeline_id, actor_entity_id, target_entity_id,
                action_type, status, payload, dag_node_id,
                domain_id, source_kind, source_ref, authority_kind, candidate_id,
                state_key, before_state, delta, result_state,
                parent_state_version, result_state_version, occurred_at
            )
            VALUES (
                $1,$2,$3,$4,$5,'accepted',$6::jsonb,$7,
                $8,$9,$10,$11,$12,$13,$14::jsonb,$15::jsonb,$16::jsonb,
                $17,$18,COALESCE($19,now())
            )
            ON CONFLICT (candidate_id) WHERE candidate_id IS NOT NULL
            DO UPDATE SET candidate_id=EXCLUDED.candidate_id
            RETURNING world_event_id
            """,
            candidate.world_id,
            candidate.timeline_id,
            candidate.entity_id,
            candidate.target_entity_id,
            candidate.event_type,
            json.dumps(_jsonable(candidate.parameters)),
            candidate.dag_node_id,
            candidate.domain_id,
            candidate.source_kind,
            candidate.source_ref,
            candidate.authority_kind,
            candidate.candidate_id,
            candidate.state_key,
            json.dumps(_jsonable(evaluation.before)),
            json.dumps(_jsonable(evaluation.delta)),
            json.dumps(_jsonable(evaluation.after)),
            before_version,
            after_version,
            candidate.occurred_at,
        )
        return row["world_event_id"]

    async def _write_state(
        self,
        con: Any,
        *,
        candidate: CausalCandidate,
        evaluation: CausalEvaluation,
        world_event_id: UUID,
        state_version: int,
    ) -> None:
        await con.execute(
            """
            INSERT INTO aios.causal_state (
                world_id,timeline_id,domain_id,entity_id,state_key,value_json,
                state_version,last_event_id,last_node_id,occurred_at,meta,updated_at
            )
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,COALESCE($10,now()),'{}'::jsonb,now())
            ON CONFLICT (world_id,timeline_id,domain_id,entity_id,state_key)
            DO UPDATE SET value_json=EXCLUDED.value_json,
                          state_version=EXCLUDED.state_version,
                          last_event_id=EXCLUDED.last_event_id,
                          last_node_id=EXCLUDED.last_node_id,
                          occurred_at=EXCLUDED.occurred_at,
                          updated_at=now()
            """,
            candidate.world_id,
            candidate.timeline_id,
            candidate.domain_id,
            candidate.entity_id,
            candidate.state_key,
            json.dumps(_jsonable(evaluation.after)),
            state_version,
            world_event_id,
            candidate.dag_node_id,
            candidate.occurred_at,
        )

    async def _record_admission(
        self,
        con: Any,
        *,
        candidate_id: UUID,
        evaluation: CausalEvaluation,
        before_version: int,
        after_version: int,
    ) -> UUID:
        row = await con.fetchrow(
            """
            INSERT INTO aios.causal_admission (
                candidate_id, decision, reason_code, reason_json,
                state_version_before, state_version_after
            )
            VALUES ($1,$2,$3,$4::jsonb,$5,$6)
            RETURNING admission_id
            """,
            candidate_id,
            evaluation.decision.value,
            evaluation.reason_code,
            json.dumps({
                "reason": evaluation.reason,
                "before": _jsonable(evaluation.before),
                "after": _jsonable(evaluation.after),
                "delta": _jsonable(evaluation.delta),
                "latent_events": _jsonable(evaluation.latent_events),
            }),
            before_version,
            after_version,
        )
        return row["admission_id"]
