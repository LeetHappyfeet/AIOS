"""Best-effort contextual lookup diagnostics, including searches that time out."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

logger = logging.getLogger(__name__)


async def record_corpus_lookup(db, context, diagnostics: dict) -> None:
    # Do not duplicate chat text in the diagnostic store. Keep routing/status
    # even when a search never reaches its ordinary research-event write.
    data = {key: diagnostics.get(key) for key in (
        "lookup_status", "maintenance_status", "query", "lookup_mode", "candidate_count", "result_count",
        "focus_node_id", "focus_speaker_id", "focus_origin", "knowledge_route",
        "demand_reason", "automatic_lookup_allowed", "reason")}
    query = str(data.get("query") or "")
    try:
        await asyncio.wait_for(db.execute(
            """INSERT INTO aios.character_corpus_lookup_trace
               (instance_id,source_node_key,query_hash,diagnostics)
               VALUES($1,$2,$3,$4::jsonb)
               ON CONFLICT(instance_id,source_node_key,query_hash)
               DO UPDATE SET diagnostics=EXCLUDED.diagnostics,updated_at=now()""",
            context.instance_id, str(getattr(context, "source_head_node_id", None) or ""),
            hashlib.sha256(query.encode()).hexdigest(), json.dumps(data, default=str)), .35)
    except Exception:
        logger.warning("Contextual corpus diagnostic unavailable instance=%s", context.instance_id, exc_info=True)
