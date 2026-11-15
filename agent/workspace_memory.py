"""Branch-scoped worker lessons, separate from chat ingestion and asserted knowledge."""

import hashlib
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aios_app.hud.text_budget import excerpt

CAPABILITY = "workspace-memory-v1"
BRIEF_CHAR_LIMIT = 1600
_LABELS = {
    "attempt": "Prior attempt (worker reported)",
    "finding": "Prior finding (worker reported)",
    "idea": "Untested idea",
}
_SECRET_OR_TRACE = re.compile(
    r"(?i)\[redacted\]|\b(?:password|api[_ -]?key|token|secret)\s*[:=]|"
    r"\bbearer\s+\S+|-----BEGIN |traceback \(most recent call last\)|```|"
    r"^(?:\$ |\[?(?:DEBUG|INFO|WARNING|ERROR)\]?\s*[:|]|[{\[])"
)


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["attempt", "finding", "idea"]
    text: str = Field(min_length=1, max_length=240)
    finding_index: int | None = Field(default=None, ge=0, le=5)
    source_url: str | None = Field(default=None, min_length=1, max_length=512)


def candidates_from_result(raw, result):
    """Validate optional nominations independently of terminal result delivery."""
    if not isinstance(raw, list):
        return []
    summary = result.get("result_summary")
    if not isinstance(summary, dict) or summary.get("source") not in {
        "publish_result", "final_response",
    }:
        return []
    findings = summary.get("findings")
    findings = findings if isinstance(findings, list) else []
    notes = {}
    for item in raw[:3]:
        try:
            candidate = MemoryCandidate.model_validate(item)
            url = urlsplit(candidate.source_url) if candidate.source_url else None
        except (ValidationError, ValueError):
            continue
        text = candidate.text.strip()
        if (
            not text or not text.isprintable() or _SECRET_OR_TRACE.search(text)
            or (url and (candidate.kind != "finding"
                         or url.scheme not in {"http", "https"} or not url.hostname
                         or url.username is not None or url.query or url.fragment
                         or not candidate.source_url.isprintable()
                         or any(ch.isspace() for ch in candidate.source_url)
                         or _SECRET_OR_TRACE.search(candidate.source_url)))
        ):
            continue
        index = candidate.finding_index
        if index is None:
            if candidate.kind != "idea":
                continue
        elif (index >= len(findings) or not isinstance(findings[index], str)
              or not findings[index].strip()
              or findings[index].startswith("[")
              or _SECRET_OR_TRACE.search(findings[index])):
            continue
        fingerprint = hashlib.sha256(
            "\0".join((candidate.kind, " ".join(text.casefold().split()),
                       candidate.source_url or "")).encode()
        ).hexdigest()
        notes.setdefault(fingerprint, candidate.model_copy(update={"text": text}))
    return list(notes.items())


async def save_notes(db, run_id, notes):
    for slot, (fingerprint, note) in enumerate(notes):
        await db.execute(
            """INSERT INTO aios.workspace_memory_note
               (run_id,slot,kind,note_text,fingerprint,finding_index,source_url)
               VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING""",
            run_id, slot, note.kind, note.text, fingerprint, note.finding_index, note.source_url,
        )


async def relevant_notes(db, instance_id, timeline_id, node_id, focus, *, goal_id=None, link_id=None):
    """Find matching notes on the current ancestry, regardless of run recency."""
    terms = dict.fromkeys(re.findall(r"[^\W_]{3,}", (focus or "")[:600].lower()))
    query = " | ".join(list(terms)[:20])
    if not timeline_id or not node_id or (not query and goal_id is None):
        return []
    rows = await db.fetch(
        """WITH RECURSIVE search(query) AS (SELECT to_tsquery('english',$4)),
        matching AS MATERIALIZED (
            SELECT n.*, w.origin_source_node_id, w.status, w.updated_at,
                   w.result->>'verification_status' AS verification_status,
                   COALESCE(w.assignment->>'goal_id'=$5::text,false) AS same_goal,
                   ts_rank(n.search_terms, search.query) AS rank
            FROM aios.workspace_memory_note n
            JOIN aios.workspace_run w USING(run_id)
            JOIN aios.character_environment_link l ON l.link_id=w.environment_link_id
                AND l.execution_binding_id=w.binding_id AND l.origin_instance_id=w.origin_instance_id
                AND l.origin_source_timeline_id=$2 AND l.enabled
            JOIN aios.workspace_binding b ON b.binding_id=w.binding_id AND b.attached
                AND b.instance_id=w.origin_instance_id AND b.timeline_id=$2
            JOIN aios.character_runtime_state rs ON rs.instance_id=w.origin_instance_id
                AND rs.source_timeline_id=$2 AND rs.source_head_node_id=$3
            JOIN aios.dag_node src ON src.node_id=w.origin_source_node_id AND src.timeline_id=$2
            LEFT JOIN aios.character_work_request wr ON wr.run_id=w.run_id
            CROSS JOIN search
            WHERE w.origin_instance_id=$1 AND w.origin_source_timeline_id=$2
              AND ($6::uuid IS NULL OR l.link_id=$6)
              AND (n.search_terms @@ search.query OR w.assignment->>'goal_id'=$5::text)
              AND (wr.request_id IS NULL OR wr.status NOT IN ('denied','invalidated'))
              AND EXISTS (SELECT 1 FROM aios.ingest_event ie
                          WHERE ie.event_id=src.event_id AND ie.superseded_at IS NULL)
        ), visible(node_id) AS (
            SELECT $3::uuid WHERE EXISTS (SELECT 1 FROM matching)
            UNION
            SELECT e.parent_node_id FROM visible v
            JOIN aios.dag_edge e ON e.child_node_id=v.node_id AND e.timeline_id=$2
        ), unique_notes AS (
            SELECT DISTINCT ON (m.fingerprint) m.* FROM matching m JOIN visible v
                ON v.node_id=m.origin_source_node_id
            ORDER BY m.fingerprint, m.same_goal DESC, m.rank DESC, m.updated_at DESC, m.run_id
        )
        SELECT run_id, slot, kind, note_text, finding_index, source_url,
               status, verification_status, updated_at FROM unique_notes
        ORDER BY same_goal DESC, rank DESC, updated_at DESC, run_id, slot LIMIT 2""",
        instance_id, timeline_id, node_id, query, str(goal_id) if goal_id else None, link_id,
    )
    return [{
        "kind": row["kind"], "text": row["note_text"], "run_id": str(row["run_id"]),
        "slot": row["slot"], "finding_index": row["finding_index"], "source_url": row["source_url"],
        "execution_status": row["status"], "verification_status": row["verification_status"],
        "observed_at": str(row["updated_at"]),
    } for row in rows]


def note_line(note, *, with_ref=False):
    from aios_app.agent.references import short_reference

    label = ("External finding (worker reported)" if note.get("source_url")
             else _LABELS[note["kind"]])
    ref = f" [{short_reference(note['run_id'], 'r')}]" if with_ref else ""
    status = note["execution_status"]
    if note.get("verification_status") in {"passed", "failed"}:
        status += f", checks {note['verification_status']}"
    return f"{label}{ref} ({status}): {note['text']}"


def render_brief(identity, goal, notes):
    lines = ["AIOS WORKER BRIEF", f"Character: {excerpt(identity[0], 100)}"]
    lines.extend(f"Decision priority: {excerpt(text, 160)}" for text in identity[1])
    if goal:
        lines.append(f"Linked goal: {excerpt(' '.join(goal.split()), 240)}")
    lines.extend(note_line(note, with_ref=True) for note in notes)
    return excerpt("\n".join(lines), BRIEF_CHAR_LIMIT)
