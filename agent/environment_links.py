"""Explicit character environments and durable task delivery.

Workers never choose an arbitrary memory owner. Operator routes use the separate
review credential; worker calls resolve identity from a leased workspace run.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_instance_id: UUID
    share_memory: bool = False


class MemorySharingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    share_memory: bool


class ProgressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_key: str
    claim_token: UUID
    event_key: UUID
    kind: str = Field(pattern="^(progress|blocked)$")
    summary: str = Field(min_length=1, max_length=1000)


class RecallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_key: str
    claim_token: UUID
    query: str = Field(min_length=1, max_length=600)


async def append_event(db, run_id, key, kind, summary, payload=None):
    """Caller holds the run lock; event and state commit in the same transaction."""
    payload = payload or {}
    old = await db.fetchrow(
        "SELECT * FROM aios.workspace_run_event WHERE run_id=$1 AND event_key=$2",
        run_id,
        key,
    )
    if old:
        from .workspaces import decode

        if (old["kind"], old["summary"], decode(old["payload"])) != (
            kind,
            summary,
            payload,
        ):
            raise HTTPException(409, "Event key reused with different content")
        return int(old["sequence"])
    return await db.fetchval(
        """INSERT INTO aios.workspace_run_event(run_id,event_key,kind,summary,payload)
           VALUES($1,$2,$3,$4,$5::jsonb) RETURNING sequence""",
        run_id,
        key,
        kind,
        summary[:1000],
        json.dumps(payload),
    )


class EnvironmentStore:
    def __init__(self, db):
        self.db = db

    async def separate_worlds(self, origin_id, execution_id):
        separate = await self.db.fetchval(
            """SELECT COALESCE(o.current_world_id,o.world_id) <> COALESCE(e.current_world_id,e.world_id)
               FROM aios.character_instance o CROSS JOIN aios.character_instance e
               WHERE o.instance_id=$1 AND e.instance_id=$2""", origin_id, execution_id)
        if not separate:
            raise HTTPException(409, "Execution requires an independent world")

    async def set_memory(self, binding_id, link_id, req):
        from .workspaces import WorkspaceStore, public
        from aios_app.epistemic.belief_reconciliation import reconcile_instance_beliefs, reconcile_character_belief_atom
        async with self.db.atomic():
            b = await WorkspaceStore(self.db).binding(binding_id, lock=True)
            await WorkspaceStore(self.db).current(b)
            link = await self.link(binding_id, link_id)
            await self.origin(link)
            if req.share_memory == link['share_memory']:
                return public(link)
            isolated = await self.db.fetchval(
                "SELECT memory_continuity='isolated' FROM aios.character_epistemic_profile WHERE character_id=$1", b['character_id'])
            if req.share_memory and isolated:
                raise HTTPException(409, "Isolated memory policy must be changed explicitly before sharing")
            updated = await self.db.execute_returning_row(
                "UPDATE aios.character_environment_link SET share_memory=$2 WHERE link_id=$1 RETURNING *", link_id, req.share_memory)
            if req.share_memory and not await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM aios.cognitive_evidence_instances($1) WHERE instance_id=$2)", b['instance_id'], link['origin_instance_id']):
                raise HTTPException(409, "Runtime memory identities do not match; reconcile before sharing")
            for instance in (link['origin_instance_id'], b['instance_id']):
                await reconcile_instance_beliefs(self.db, instance_id=instance)
                if not req.share_memory:
                    old = await self.db.fetch("SELECT atom_id FROM aios.character_belief_state WHERE instance_id=$1", instance)
                    for belief in old:
                        await reconcile_character_belief_atom(self.db, instance_id=instance, atom_id=belief['atom_id'])
            return public(updated)

    async def create(self, binding_id, req):
        from .workspaces import WorkspaceStore, public

        async with self.db.atomic():
            b = await WorkspaceStore(self.db).binding(binding_id, lock=True)
            await WorkspaceStore(self.db).current(b)
            origin = await self.db.fetchrow(
                """SELECT ci.character_id,rs.instance_id,rs.source_timeline_id,
                          rt.session_id,rt.user_name,rt.scope_key,
                          st.session_id AS source_session_id,st.user_name AS source_user_name,
                          st.character_id AS source_character_id,st.scope_key AS source_scope_key,
                          COALESCE(p.memory_continuity,'character') AS memory_continuity
                   FROM aios.character_runtime_state rs
                   JOIN aios.character_instance ci USING(instance_id)
                   JOIN aios.timeline rt ON rt.timeline_id=rs.timeline_id
                   JOIN aios.timeline st ON st.timeline_id=rs.source_timeline_id
                   LEFT JOIN aios.character_epistemic_profile p ON p.character_id=ci.character_id
                   WHERE rs.instance_id=$1 FOR UPDATE OF rs""",
                req.origin_instance_id,
            )
            if (
                not origin
                or origin["instance_id"] == b["instance_id"]
                or origin["character_id"] != b["character_id"]
                or origin["user_name"] != b["user_name"]
                or origin["session_id"] is None
                or origin["source_timeline_id"] == b["timeline_id"]
                or any(
                    origin["source_" + k] != origin[k]
                    for k in ("session_id", "user_name", "character_id", "scope_key")
                )
            ):
                raise HTTPException(
                    409,
                    "Link requires separate sources for the same character and runtime user",
                )
            await self.separate_worlds(origin["instance_id"], b["instance_id"])
            if req.share_memory and origin["memory_continuity"] == "isolated":
                raise HTTPException(
                    409,
                    "Isolated memory policy must be changed explicitly before sharing",
                )
            old = await self.db.fetchrow(
                """SELECT * FROM aios.character_environment_link
                   WHERE origin_instance_id=$1 AND execution_binding_id=$2 FOR UPDATE""",
                req.origin_instance_id,
                binding_id,
            )
            if old:
                if (
                    not old["enabled"]
                    or old["share_memory"] != req.share_memory
                    or old["origin_source_timeline_id"] != origin["source_timeline_id"]
                ):
                    raise HTTPException(
                        409, "Environment link is revoked or has different settings"
                    )
                return public(old)
            row = await self.db.execute_returning_row(
                """INSERT INTO aios.character_environment_link
                   (origin_instance_id,execution_instance_id,execution_binding_id,
                    origin_session_id,origin_source_timeline_id,share_memory)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING *""",
                req.origin_instance_id,
                b["instance_id"],
                binding_id,
                origin["session_id"],
                origin["source_timeline_id"],
                req.share_memory,
            )
            # Existing materialized winners must see the newly eligible evidence.
            if req.share_memory:
                from aios_app.epistemic.belief_reconciliation import (
                    reconcile_instance_beliefs,
                )

                visible = await self.db.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM aios.cognitive_evidence_instances($1) WHERE instance_id=$2)",
                    b["instance_id"],
                    req.origin_instance_id,
                )
                if not visible:
                    raise HTTPException(
                        409,
                        "Runtime memory identities do not match; reconcile before sharing",
                    )
                await reconcile_instance_beliefs(
                    self.db, instance_id=req.origin_instance_id
                )
                await reconcile_instance_beliefs(self.db, instance_id=b["instance_id"])
            return public(row)

    async def link(self, binding_id, link_id, *, enabled=True):
        row = await self.db.fetchrow(
            """SELECT * FROM aios.character_environment_link
               WHERE execution_binding_id=$1 AND link_id=$2 FOR UPDATE""",
            binding_id,
            link_id,
        )
        if not row or (enabled and not row["enabled"]):
            raise HTTPException(409, "Unknown or revoked environment link")
        return row

    async def origin(self, link, source_node_id=None, ancestor_node_id=None):
        row = await self.db.fetchrow(
            """SELECT rs.source_timeline_id,rs.source_head_node_id,t.session_id,t.user_name,
                      ci.character_id
               FROM aios.character_runtime_state rs
               JOIN aios.character_instance ci USING(instance_id)
               JOIN aios.timeline t ON t.timeline_id=rs.timeline_id
               WHERE rs.instance_id=$1 FOR UPDATE OF rs""",
            link["origin_instance_id"],
        )
        b = await self.db.fetchrow(
            "SELECT * FROM aios.workspace_binding WHERE binding_id=$1",
            link["execution_binding_id"],
        )
        if (
            not row
            or row["source_timeline_id"] != link["origin_source_timeline_id"]
            or row["session_id"] != link["origin_session_id"]
            or row["character_id"] != b["character_id"]
            or row["user_name"] != b["user_name"]
            or (
                source_node_id is not None
                and row["source_head_node_id"] != source_node_id
            )
        ):
            raise HTTPException(
                409, "Origin conversation changed; reconcile explicitly"
            )
        await self.separate_worlds(link["origin_instance_id"], b["instance_id"])
        if ancestor_node_id is not None:
            current = await self.db.fetchval(
                """WITH RECURSIVE ancestry(node_id,depth) AS (
                   SELECT node_id,0 FROM aios.dag_node WHERE node_id=$1 AND timeline_id=$2
                   UNION SELECT e.parent_node_id,a.depth+1 FROM ancestry a
                   JOIN aios.dag_edge e ON e.child_node_id=a.node_id AND e.timeline_id=$2
                   WHERE a.depth<256)
                   SELECT EXISTS(SELECT 1 FROM ancestry WHERE node_id=$3)""",
                row["source_head_node_id"],
                row["source_timeline_id"],
                ancestor_node_id,
            )
            if not current:
                raise HTTPException(
                    409,
                    "Delegation source is no longer on the active conversation branch",
                )
        return row

    async def delegate(self, binding_id, link_id, req):
        from .workspaces import WorkspaceStore

        async with self.db.atomic():
            store = WorkspaceStore(self.db)
            await store.binding(binding_id, lock=True)
            link = await self.link(binding_id, link_id)
            # Idempotent submission survives later chat turns.
            old = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_run WHERE binding_id=$1 AND request_id=$2",
                binding_id,
                req.request_id,
            )
            if old:
                if (
                    old["environment_link_id"] != link_id
                    or old["origin_source_node_id"] != req.source_node_id
                    or old["assignment_hash"] != req.spec().digest()
                ):
                    raise HTTPException(
                        409, "Request ID reused with different delegation"
                    )
                from .workspaces import public

                return public(old)
            await self.origin(link, req.source_node_id)
            return await store.submit(binding_id, req, environment=link)

    async def progress(self, binding_id, run_id, req):
        from .workspaces import WorkspaceStore

        async with self.db.atomic():
            b, _run = await WorkspaceStore(self.db).owned(binding_id, run_id, req)
            await WorkspaceStore(self.db).current(b)
            return {
                "sequence": await append_event(
                    self.db, run_id, str(req.event_key), req.kind, req.summary
                )
            }

    async def recall(self, binding_id, run_id, req):
        from aios_app.epistemic.inquiry.contracts import InquiryDemand
        from aios_app.epistemic.inquiry.lookup import InquiryLookup

        from .workspaces import WorkspaceStore

        store = WorkspaceStore(self.db)
        async with self.db.atomic():
            b, _ = await store.owned(binding_id, run_id, req)
            await store.current(b)
            revision = await self.db.fetchval(
                "SELECT aios.cognitive_context_revision($1)", b["instance_id"]
            )
        demand = InquiryDemand(
            instance_id=b["instance_id"],
            origin="workspace",
            uncertainty_kind="episodic_gap",
            question=req.query,
            evidence_revision=revision,
            source_node_id=b["last_node_id"],
        )
        try:
            evidence = await asyncio.wait_for(
                InquiryLookup(self.db).search(demand, target="personal"), 5
            )
        except (asyncio.TimeoutError, ConnectionError):  # noqa: UP041 -- Python 3.10
            return {"status": "unavailable", "hits": [], "knowledge_revision": revision}
        # Revocation or a changed evidence scope during lookup must not leak old hits.
        async with self.db.atomic():
            await store.owned(binding_id, run_id, req)
            after = await self.db.fetchval(
                "SELECT aios.cognitive_context_revision($1)", b["instance_id"]
            )
            if after != revision:
                return {"status": "changed", "hits": [], "knowledge_revision": after}
        return {
            **asdict(evidence),
            "hits": [hit.as_dict() for hit in evidence.hits],
            "knowledge_revision": revision,
        }

    async def cancel(self, binding_id, run_id):
        from .workspaces import WorkspaceStore, public

        async with self.db.atomic():
            await WorkspaceStore(self.db).binding(binding_id, lock=True)
            row = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_run WHERE binding_id=$1 AND run_id=$2 FOR UPDATE",
                binding_id,
                run_id,
            )
            if not row:
                raise HTTPException(404, "Unknown run")
            if row["status"] not in {"queued", "running"} or row["cancel_requested"]:
                return public(row)
            queued = row["status"] == "queued"
            result = await self.db.execute_returning_row(
                """UPDATE aios.workspace_run SET cancel_requested=true,
                   status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated_at=now()
                   WHERE run_id=$1 RETURNING *""",
                run_id,
            )
            if queued:
                await self.db.execute(
                    "UPDATE aios.character_action SET status='cancelled',completed_at=now() WHERE action_id=$1",
                    row["action_id"],
                )
            await append_event(
                self.db,
                run_id,
                "operator:cancel",
                "cancelled" if queued else "cancel_requested",
                "Assignment cancelled"
                if queued
                else "Cancellation requested; worker may still be stopping",
            )
            return public(result)

    async def revoke(self, binding_id, link_id):
        from .workspaces import WorkspaceStore

        async with self.db.atomic():
            await WorkspaceStore(self.db).binding(binding_id, lock=True)
            link = await self.link(binding_id, link_id, enabled=False)
            await self.db.execute(
                "UPDATE aios.character_environment_link SET enabled=false,revoked_at=COALESCE(revoked_at,now()) WHERE link_id=$1",
                link_id,
            )
            runs = await self.db.fetch(
                "SELECT run_id FROM aios.workspace_run WHERE environment_link_id=$1 AND status IN ('queued','running') ORDER BY run_id",
                link_id,
            )
            for run in runs:
                await self.cancel(binding_id, run["run_id"])
            if link["share_memory"]:
                from aios_app.epistemic.belief_reconciliation import (
                    reconcile_character_belief_atom,
                    reconcile_instance_beliefs,
                )

                for instance in (
                    link["origin_instance_id"],
                    link["execution_instance_id"],
                ):
                    await reconcile_instance_beliefs(self.db, instance_id=instance)
                    # Include winners whose only evidence just became ineligible.
                    old = await self.db.fetch(
                        "SELECT atom_id FROM aios.character_belief_state WHERE instance_id=$1",
                        instance,
                    )
                    for belief in old:
                        await reconcile_character_belief_atom(
                            self.db, instance_id=instance, atom_id=belief["atom_id"]
                        )
            return {"link_id": str(link_id), "enabled": False}


async def task_overlay(db, instance_id, timeline_id):
    """Cheap pull delivery: independent of source advancement and semantic jobs."""
    rows = await db.fetch(
        """SELECT r.run_id,r.status,r.cancel_requested,r.objective,r.result,r.origin_source_node_id,rv.status AS review_status,e.sequence,e.kind,e.summary,e.created_at
           FROM aios.workspace_run r
           JOIN aios.character_environment_link l ON l.link_id=r.environment_link_id AND l.enabled
           LEFT JOIN aios.workspace_review rv ON rv.run_id=r.run_id
           LEFT JOIN LATERAL (SELECT * FROM aios.workspace_run_event ev WHERE ev.run_id=r.run_id
                              ORDER BY sequence DESC LIMIT 1) e ON true
           WHERE r.origin_instance_id=$1 AND r.origin_source_timeline_id=$2
           ORDER BY (r.status IN ('queued','running')) DESC,r.updated_at DESC,r.run_id LIMIT 4""",
        instance_id,
        timeline_id,
    )
    from .workspaces import decode

    return [
        {
            "run_id": str(r["run_id"]),
            "source_node_id": str(r["origin_source_node_id"]),
            "status": r["status"],
            "cancel_requested": r["cancel_requested"],
            "objective": r["objective"][:240],
            "sequence": r["sequence"],
            "kind": r["kind"],
            "summary": r["summary"],
            "verification_status": (decode(r["result"]) or {}).get(
                "verification_status"
            ),
            "goal_review": r["review_status"]
            or (decode(r["result"]) or {}).get("goal_review"),
            "reported_at": str(r["created_at"]) if r["created_at"] else None,
        }
        for r in rows
    ]
