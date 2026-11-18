"""Wall-clock ages for already-visible HUD evidence."""
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aios_app.config import settings


def age_label(now, when, label):
    if not isinstance(when, datetime) or when.tzinfo is None or when > now:
        return ""
    seconds = int((now - when).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{label} {seconds // size}{unit} ago"
    return f"{label} <1m ago"


def _node(item):
    value = item.get("source_node_id") or item.get("node_id")
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


async def attach_time_references(db, events, memories, *, timeline_id=None, head_node_id=None):
    node_ids = list({node for item in [*events, *memories] if (node := _node(item))})
    sources = {}
    if node_ids:
        rows = await db.fetch(
            "SELECT node_id,event_time,created_at FROM aios.dag_node WHERE node_id=ANY($1::uuid[])",
            node_ids,
        )
        sources = {row["node_id"]: row for row in rows}
    experience = {}
    if node_ids and timeline_id and head_node_id:
        ancestors = await db.fetch(
            """WITH RECURSIVE ancestors(node_id,depth) AS (
                   SELECT $2::uuid,0
                   UNION
                   SELECT e.parent_node_id,a.depth+1 FROM ancestors a
                   JOIN aios.dag_edge e ON e.child_node_id=a.node_id AND e.timeline_id=$1
                   WHERE a.depth<64 AND e.edge_type='next'
               )
               SELECT node_id,min(depth) AS depth FROM ancestors
               WHERE node_id=ANY($3::uuid[]) GROUP BY node_id""", timeline_id, head_node_id, node_ids,
        )
        experience = {row["node_id"]: row["depth"] for row in ancestors}
    for item in [*events, *memories]:
        node = _node(item)
        source = sources.get(node, {})
        if node in experience:
            depth = experience[node]
            item["experience_position"] = "branch head" if depth == 0 else f"{depth} source steps earlier on branch"
        event_time = item.get("event_time") or source.get("event_time")
        if isinstance(event_time, datetime) and event_time.tzinfo is not None:
            item["time_reference"] = {"kind": "source time", "at": event_time.isoformat()}
        elif source.get("created_at"):
            item["time_reference"] = {"kind": "recorded", "at": source["created_at"].isoformat()}


async def timeline_hud(db, instance_id, source_timeline_id, events, memories):
    now = datetime.now(timezone.utc)
    row = await db.fetchrow(
        "SELECT meta->>'timezone' AS timezone FROM aios.character_instance WHERE instance_id=$1",
        instance_id,
    )
    zone_name = (row or {}).get("timezone") or settings.default_timezone or "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        zone = timezone.utc

    for index, item in enumerate(events):
        item["time_label"] = display_time(now, item) or (
            "latest in feed" if index == 0 else "earlier in feed"
        )
    for item in memories:
        item["time_label"] = display_time(now, item)

    due = await db.fetch(
        """SELECT reason,status,due_at,last_outcome,payload->>'decision_note' AS note
           FROM aios.character_temporal_trigger
           WHERE instance_id=$1 AND payload ? 'reminder_goal_id'
             AND payload->>'source_timeline_id'=$2
             AND (status IN ('scheduled','firing','fired')
                  OR (status='completed' AND last_evaluated_at > now()-interval '1 day'))
           ORDER BY CASE status WHEN 'fired' THEN 0 WHEN 'firing' THEN 0
                                WHEN 'scheduled' THEN 1 ELSE 2 END,
                    CASE WHEN status='completed' THEN last_evaluated_at END DESC,due_at
           LIMIT 3""", instance_id, str(source_timeline_id) if source_timeline_id else None,
    )
    lines = [f"Wall time: {now.astimezone(zone).isoformat(timespec='minutes')} ({zone})"]
    for item in due:
        due_at = item["due_at"].astimezone(zone).isoformat(timespec="seconds")
        reason = " ".join(str(item["reason"] or "").split())[:120]
        if item["status"] == "completed":
            note = " ".join(str(item["note"] or "").split())[:120]
            lines.append(f"Reminder {item['last_outcome']}: {reason}. {note}")
        else:
            status = "due for review" if item["due_at"] <= now else "scheduled"
            if item["due_at"] < now:
                status += f" ({int((now - item['due_at']).total_seconds()) // 60}m overdue)"
            lines.append(f"Reminder {status} at {due_at}: {reason}")
    return lines


def reference_age(now, item):
    reference = item.get("time_reference")
    if not reference:
        return ""
    try:
        at = datetime.fromisoformat(reference["at"])
    except (TypeError, ValueError):
        return ""
    return age_label(now, at, reference["kind"])


def display_time(now, item):
    return " | ".join(filter(None, (item.get("experience_position"), reference_age(now, item))))
