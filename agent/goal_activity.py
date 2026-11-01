"""Scheduling urgency only; this module never grants execution authority."""
from __future__ import annotations

import hashlib
import json


async def observe_interaction(db, instance_id):
    """Call only for genuinely new adopted external experience, never worker IO."""
    await db.execute(
        """INSERT INTO aios.instance_goal_activity(instance_id,last_interaction_at)
           VALUES($1,now()) ON CONFLICT(instance_id) DO UPDATE
           SET last_interaction_at=EXCLUDED.last_interaction_at""", instance_id)
    # Resume existing needs immediately; neither recreate plans nor discard goals.
    await db.execute(
        """UPDATE aios.character_goal_plan
           SET continuation_check_at=now(),continuation_signature=NULL,
               continuation_unchanged_checks=0
           WHERE instance_id=$1 AND status='waiting_information'""", instance_id)


async def eligible(db, instance_id):
    return bool(await db.fetchval(
        "SELECT eligible FROM aios.instance_goal_scheduling WHERE instance_id=$1", instance_id))


def continuation_retry(previous_signature, unchanged_checks, *, source, requirements, discoveries):
    """Stable evidence coordinates count as change; diagnostic timings do not."""
    signature = hashlib.sha256(json.dumps(
        {"source": str(source), "requirements": requirements,
         "discoveries": {key: value[0] for key, value in sorted(discoveries.items())}},
        sort_keys=True, default=str).encode()).hexdigest()
    unchanged = min(8, unchanged_checks + 1) if signature == previous_signature else 0
    return signature, unchanged, min(900, 60 * (2 ** unchanged))


async def grant_scheduled_window(db, instance_id):
    """An actual due timer gets a bounded window, without simulating a chat turn."""
    await db.execute(
        """INSERT INTO aios.instance_goal_activity(instance_id,explicit_work_until)
           VALUES($1,now()+interval '15 minutes') ON CONFLICT(instance_id) DO UPDATE
           SET explicit_work_until=EXCLUDED.explicit_work_until""", instance_id)


async def set_background(db, instance_id, enabled):
    await db.execute(
        """INSERT INTO aios.instance_goal_activity(instance_id,background_enabled)
           VALUES($1,$2) ON CONFLICT(instance_id) DO UPDATE
           SET background_enabled=EXCLUDED.background_enabled""", instance_id, bool(enabled))
