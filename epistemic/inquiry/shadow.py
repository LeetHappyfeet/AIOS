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
    if not row or not row["enrichment_completed_at"]:
        return []
    summary = _object(row["summary"])
    excerpts = summary.get("ambiguous_sentences") or []
    outputs = []
    service = CharacterInquiryService(db)
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
