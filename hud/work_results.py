"""Bounded, deterministic HUD projection of untrusted worker results.

The original result stays in workspace_run.result. Neither a worker outcome nor
evidence completeness is an independent assertion that the objective succeeded.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping


def compact(value, limit=240):
    if not isinstance(value, (str, int, float, bool)):
        return ""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _items(value, count=3, limit=180):
    if not isinstance(value, list):
        return []
    items = [compact(json.dumps(v, ensure_ascii=False) if isinstance(v, (Mapping, list)) else v, limit)
             for v in value[:count]]
    items = [v for v in items if v]
    if len(value) > count:
        items.append(f"… {len(value) - count} more entries omitted")
    return items


def _report_excerpt(value):
    # Markdown tables cannot survive a short prose excerpt. Keep earlier prose
    # or explicitly direct the consumer to the retained report instead.
    if not isinstance(value, str):
        return ""
    prose = []
    for line in value.splitlines():
        if line.strip().startswith(("|", "```")):
            break
        prose.append(line)
    text = compact(" ".join(prose), 240)
    if len(prose) < len(value.splitlines()) and text and not text.endswith("…"):
        text = compact(text, 238) + " …"
    return text or "Structured/table report retained in run details."


def _report_only(checks):
    # Match the operator preset's actual command, never a worker's scope label
    # or a check ID alone. UUID substitution is the only template expansion.
    if not checks:
        return False
    from aios_app.agent.work_profiles import experimental_profile
    expected = experimental_profile().assignment_template["required_checks"][0]
    pattern = re.escape(expected["command"]).replace(
        re.escape("{request_id}"), r"(?:\{request_id\}|[0-9a-fA-F-]{36})"
    )
    return bool(checks) and all(
        isinstance(c, Mapping) and c.get("id") == expected["id"]
        and re.fullmatch(pattern, str(c.get("command") or ""))
        for c in checks
    )


def result_view(result, assignment):
    result = result if isinstance(result, Mapping) else {}
    assignment = assignment if isinstance(assignment, Mapping) else {}
    summary = result.get("result_summary")
    if not isinstance(summary, Mapping):
        summary = result.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    evidence = result.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    checks = assignment.get("required_checks") or []
    scope = ("Report structure only; task success is not established."
             if _report_only(checks) else
             "Configured checks only; task success is not independently established."
             if checks else "No configured checks; task success is not established.")
    actual = evidence.get("checks")
    actual = actual if isinstance(actual, list) else []
    check_details = []
    for required in checks[:4]:
        if not isinstance(required, Mapping):
            continue
        entry = next((c for c in actual if isinstance(c, Mapping)
                      and c.get("id") == required.get("id")), {})
        check_details.append({
            "id": compact(required.get("id"), 80),
            "exit_code": entry.get("exit_code") if type(entry.get("exit_code")) is int else None,
            "timed_out": entry.get("timed_out") is True,
            "cancelled": entry.get("cancelled") is True,
        })
    budget = result.get("context_budget") or result.get("context_budget_breakdown")
    budget = budget if isinstance(budget, Mapping) else {}
    budget_text = compact(json.dumps(dict(list(budget.items())[:8]), ensure_ascii=False), 360) if budget else ""
    artifacts = summary.get("artifacts", result.get("artifacts"))
    if artifacts is None:
        artifacts = evidence.get("outputs")
    content = summary.get("requested_content")
    content = content if isinstance(content, list) else []
    sections = []
    for section in content[:4]:
        if not isinstance(section, Mapping) or not isinstance(section.get("text"), str):
            continue
        text = section["text"]
        sections.append({"label": compact(section.get("label"), 160) or "Requested content",
                         "text": text[:8000],
                         "truncated": section.get("truncated") is True or len(text) > 8000})
    return {
        "requested_content": sections,
        "content_sections_omitted": max(0, len(content) - 4) + (
            summary.get("content_sections_omitted", 0)
            if type(summary.get("content_sections_omitted")) is int
            and summary.get("content_sections_omitted", 0) > 0 else 0),
        "transport_truncated": summary.get("transport_truncated") is True,
        "report_body": result.get("report", "")[:6000] if isinstance(result.get("report"), str) else "",
        "report_body_truncated": result.get("report_truncated") is True or (isinstance(result.get("report"), str) and len(result["report"]) > 6000),
        "worker_outcome": compact(summary.get("outcome") or result.get("outcome"), 80) or "not supplied",
        "summary": compact(summary.get("summary") or summary.get("outcome_summary"), 1000),
        "findings": _items(summary.get("findings", result.get("findings")), 6, 700),
        "limitations": _items(summary.get("limitations", result.get("limitations")), 4, 220),
        "artifacts": _items(artifacts),
        "worker_verification_scope": compact(summary.get("verification_scope") or result.get("verification_scope")),
        "verification_scope": scope,
        "checks": check_details,
        "checks_omitted": max(0, len(checks) - 4),
        "violations": _items(evidence.get("violations"), 3, 180),
        "error": compact(result.get("error"), 360),
        "context_budget": budget_text,
        "report_excerpt": _report_excerpt(result.get("report")),
        "evidence_source": "worker-supplied; not independent host observation",
    }
