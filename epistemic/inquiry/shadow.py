"""Post-enrichment V11 diagnostic consumer. Never blocks parsing or re-admits goals."""
from __future__ import annotations
import json
from typing import Any
from uuid import UUID
from .trigger import demand_from_v11_rejection
from .service import CharacterInquiryService


def _object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except ValueError:
            pass
    return {}


async def scan_v11_rejections(db: Any, *, instance_id: UUID,
                              node_id: UUID) -> list[dict]:
    """Only persisted, completed diagnostics. No inference or parser imports."""
    row = await db.fetchrow(
        """SELECT c.summary,c.enrichment_completed_at
           FROM aios.message_cognitive_commit c
           WHERE c.instance_id=$1 AND c.node_id=$2""",
        instance_id, node_id)
    if not row:
        return []
    summary = _object(row["summary"])
    excerpts = summary.get("ambiguous_sentences") or []
    outputs = []
    service = CharacterInquiryService(db)
    # Fast V11 candidate diagnostics are already persisted before any optional
    # enrichment. The source excerpt is the rejected original, not a GOAL rewrite.
    for rejected in (summary.get("candidate_rejections") or [])[:32]:
        if not isinstance(rejected, dict):
            continue
        index = rejected.get("sentence_index")
        source = str(rejected.get("source_excerpt") or "")
        if not isinstance(index, int) or not source:
            continue
        demand = demand_from_v11_rejection(
            instance_id=instance_id, source_node_id=node_id, source_text=source,
            rejection_reason=str(rejected.get("reason") or ""), source_index=index,
            admission_version=str(summary.get("interpreter_version") or
                                  "goal-source-admission-v1"))
        if demand:
            outputs.append(await service.resolve(demand, allow_model=False))
    if not row["enrichment_completed_at"]:
        return outputs
    for rejected in (summary.get("enrichment_rejections") or [])[:4]:
        if not isinstance(rejected, dict):
            continue
        index = rejected.get("source_index")
        if not isinstance(index, int) or not (0 <= index < len(excerpts)):
            continue
        demand = demand_from_v11_rejection(
            instance_id=instance_id, source_node_id=node_id,
            source_text=str(excerpts[index]),
            rejection_reason=str(rejected.get("reason") or ""),
            source_index=index,
            admission_version=str(rejected.get("admission_version") or
                                  summary.get("enrichment_admission_version") or
                                  "goal-source-admission-v1"))
        if demand:
            outputs.append(await service.resolve(demand, allow_model=False))
    return outputs


async def enqueue_v11_inquiry_shadow(db: Any, *, limit: int = 8) -> int:
    """Independent background discovery: ingestion never waits for retrieval.

    Scan only eligible parser diagnostics without an inquiry receipt or queued
    shadow job. Stable evidence fingerprints make replays idempotent.
    """
    from aios_app.pipeline.jobs import enqueue_job
    rows = await db.fetch(
        """SELECT c.instance_id,c.node_id
           FROM aios.message_cognitive_commit c
           WHERE (
             c.summary->'candidate_rejections' @>
                 '[{"reason":"unresolved_reference:objective_contains_unresolved_reference"}]'::jsonb
             OR c.summary->'candidate_rejections' @>
                 '[{"reason":"attribution_unresolved:missing_source_text"}]'::jsonb
           )
           AND NOT EXISTS (
             SELECT 1 FROM aios.character_inquiry iq
              WHERE iq.instance_id=c.instance_id
                AND iq.source_node_id=c.node_id
                AND iq.origin='message_cognition'
                AND iq.policy_version=COALESCE(c.summary->>'interpreter_version','')
           )
           AND NOT EXISTS (
             SELECT 1 FROM aios.pipeline_job pj
              WHERE pj.job_type='character_inquiry_shadow'
                AND pj.payload->>'instance_id'=c.instance_id::text
                AND pj.payload->>'node_id'=c.node_id::text
                AND pj.status IN ('queued','running')
           )
           ORDER BY c.committed_at DESC LIMIT $1""",
        max(1, min(int(limit), 16)))
    count = 0
    for row in rows:
        job_id = await enqueue_job(
            db, job_type="character_inquiry_shadow",
            payload={"instance_id": str(row["instance_id"]),
                     "node_id": str(row["node_id"])}, priority=35)
        count += int(job_id is not None)
    return count
