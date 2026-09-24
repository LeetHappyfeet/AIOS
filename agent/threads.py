from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database


class CognitiveThreadService:
    """Longitudinal subject/problem state above disposable cognitive opportunities."""

    def __init__(self, db: Database):
        self.db = db

    async def cross(self, opportunity: Mapping[str, Any]) -> UUID:
        instance_id = UUID(str(opportunity["instance_id"]))
        subject_key = str(opportunity.get("supersession_key") or opportunity["opportunity_id"])
        strength = max(0.0, min(1.0, float(opportunity.get("priority_score") or 0.0) / 10.0))
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.character_cognitive_thread(
                   instance_id,subject_key,pressure,current_question,source_timeline_id,
                   last_source_node_id,last_state_version,crossing_count,meta)
               VALUES($1,$2,0,$3,$4,$5,$6,0,$7::jsonb)
               ON CONFLICT(instance_id,subject_key) DO UPDATE SET
                   status=CASE WHEN aios.character_cognitive_thread.status='resolved'
                               THEN 'open' ELSE aios.character_cognitive_thread.status END,
                   current_question=EXCLUDED.current_question,
                   source_timeline_id=EXCLUDED.source_timeline_id,
                   last_source_node_id=EXCLUDED.last_source_node_id,
                   last_state_version=EXCLUDED.last_state_version,
                   updated_at=now()
               RETURNING thread_id""",
            instance_id, subject_key,
            str(opportunity.get("natural_language") or "")[:1000],
            opportunity.get("source_timeline_id"), opportunity.get("source_node_id"),
            opportunity.get("source_state_version"),
            json.dumps({"last_operation_type": opportunity.get("operation_type")}, default=str),
        )
        thread_id = row["thread_id"]
        crossing=await self.db.execute_returning_row(
            """INSERT INTO aios.character_cognitive_thread_crossing(
                   thread_id,opportunity_id,source_node_id,crossing_type,evidence,strength)
               VALUES($1,$2,$3,$4,$5::jsonb,$6)
               ON CONFLICT(thread_id,opportunity_id) DO NOTHING
               RETURNING crossing_id""",
            thread_id, opportunity["opportunity_id"], opportunity.get("source_node_id"),
            str(opportunity.get("opportunity_type") or "unknown"),
            json.dumps(opportunity.get("evidence") or [], default=str), strength,
        )
        if crossing:
            await self.db.execute(
                """UPDATE aios.character_cognitive_thread
                   SET pressure=LEAST(10.0,pressure+$2),crossing_count=crossing_count+1,
                       last_crossed_at=now(),updated_at=now()
                   WHERE thread_id=$1""",thread_id,strength)
        return thread_id
