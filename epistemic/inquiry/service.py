"""Audited, non-blocking inquiry lifecycle; search/exposure never admits memory."""
from __future__ import annotations
import json
from typing import Any
from uuid import UUID

from .contracts import InquiryDemand, InquiryEvidence, InquiryHit
from .lookup import InquiryLookup
from .planner import InquiryQueryPlanner
from .trigger import should_escalate
from aios_app.hud.context import HUDContextResolver


def _object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


class CharacterInquiryService:
    def __init__(self, db: Any):
        self.db = db
        self.lookup = InquiryLookup(db)

    async def resolve(self, demand: InquiryDemand, *,
                      allow_model: bool = False) -> dict:
        """Persist one evidence-relative inquiry. No LLM in this worker/lane."""
        row = await self.db.fetchrow(
            """INSERT INTO aios.character_inquiry (
                 instance_id,source_node_id,demand_fingerprint,origin,
                 uncertainty_kind,evidence_scope,evidence_revision,policy_version,
                 demand,status)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,'queued')
               ON CONFLICT (instance_id,demand_fingerprint)
               DO UPDATE SET updated_at=now()
               RETURNING inquiry_id,status,result,model_calls""",
            demand.instance_id, demand.source_node_id, demand.fingerprint,
            demand.origin, demand.uncertainty_kind, demand.evidence_scope,
            demand.evidence_revision, demand.policy_version,
            json.dumps(demand.as_dict()))
        inquiry_id = row["inquiry_id"]
        if row["status"] != "queued":
            eligible = bool(allow_model and demand.evidence_scope != "source_local" and
                            should_escalate(demand, str(row["status"]),
                                            existing_model_calls=int(row["model_calls"])))
            return {"inquiry_id": str(inquiry_id), "status": row["status"],
                    "result": _object(row["result"]), "cached": True,
                    "plan_eligible": eligible}
        evidence = await self.lookup.search(demand)
        # Only an unanswered character-level question permits bounded fallback.
        # Source-local V11 repair cannot consult subjective or reference memory.
        if (evidence.status == "unresolved" and
                demand.evidence_scope == "character_accessible" and
                demand.uncertainty_kind in {"explicit_question", "failed_retrieval",
                                           "ambiguous_meaning"}):
            for target in ("world", "reference"):
                alternative = await self.lookup.search(demand, target=target)
                if alternative.hits:
                    evidence = alternative
                    break
        data = evidence.as_dict()
        updated = await self.db.fetchrow(
            """UPDATE aios.character_inquiry
                  SET status=$2,result=$3::jsonb,updated_at=now()
                WHERE inquiry_id=$1 AND status='queued'
                RETURNING inquiry_id""",
            inquiry_id, evidence.status, json.dumps(data))
        if not updated:
            return {"inquiry_id": str(inquiry_id), "status": "already_claimed",
                    "result": data, "plan_eligible": False}
        # V11 source diagnostics stay deterministic/read-only in shadow mode.
        eligible = bool(allow_model and demand.evidence_scope != "source_local" and
                        should_escalate(demand, evidence.status))
        return {"inquiry_id": str(inquiry_id), "status": evidence.status,
                "result": data, "cached": False, "plan_eligible": eligible}

    async def claim_plan(self, inquiry_id: UUID) -> bool:
        row = await self.db.fetchrow(
            """UPDATE aios.character_inquiry SET status='planning',model_calls=1,
                      updated_at=now()
                WHERE inquiry_id=$1 AND evidence_scope='character_accessible'
                  AND status IN ('partial','unresolved') AND model_calls=0
                RETURNING inquiry_id""", inquiry_id)
        return bool(row)

    async def plan_and_retry(self, inquiry_id: UUID) -> dict:
        """Separate inference-lane operation, at most one model pass and one retry."""
        row = await self.db.fetchrow(
            "SELECT * FROM aios.character_inquiry WHERE inquiry_id=$1 AND status='planning'",
            inquiry_id)
        if not row:
            return {"status": "not_planning"}
        demand = InquiryDemand.from_dict(_object(row["demand"]))
        prior = _object(row["result"])
        evidence = InquiryEvidence(
            status=str(prior.get("status") or "unresolved"),
            hits=tuple(InquiryHit(
                source=str(h.get("source") or ""), evidence_id=str(h.get("evidence_id") or ""),
                text=str(h.get("text") or ""), provenance=_object(h.get("provenance")),
                durable_knowledge=bool(h.get("durable_knowledge", False)))
                for h in (prior.get("hits") or [])[:3] if isinstance(h, dict)),
            reason=str(prior.get("reason") or ""))
        try:
            ctx = await HUDContextResolver(self.db).resolve(demand.instance_id)
            proposal = await InquiryQueryPlanner(self.db).plan(
                demand, evidence, character_id=ctx.character_id)
            new_evidence = await self.lookup.search(
                demand, query=proposal.query, target=proposal.target, limit=3)
            receipt = {"initial": prior, "retry": new_evidence.as_dict(),
                       "proposal": {"question": proposal.question,
                                    "query": proposal.query, "target": proposal.target},
                       "inference_request_id": proposal.request_id,
                       "admission_effect": "none"}
            status = new_evidence.status
        except Exception as exc:
            # Failure is terminal for this revision; never requeue endlessly.
            receipt = {"initial": prior, "planner_error": type(exc).__name__,
                       "admission_effect": "none"}
            status = str(prior.get("status") or "unresolved")
        await self.db.execute(
            """UPDATE aios.character_inquiry
                  SET status=$2,result=$3::jsonb,updated_at=now()
                WHERE inquiry_id=$1 AND status='planning'""",
            inquiry_id, status, json.dumps(receipt))
        return {"inquiry_id": str(inquiry_id), "status": status, "result": receipt}
