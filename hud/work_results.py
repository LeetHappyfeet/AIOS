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
    from aios_app.agent.work_profiles import workspace_task_profile

    for legacy_report in (False, True):
        expected = workspace_task_profile(legacy_report=legacy_report).assignment_template["required_checks"][0]
        pattern = re.escape(expected["command"]).replace(
            re.escape("{request_id}"), r"(?:\{request_id\}|[0-9a-fA-F-]{36})"
        )
        if all(isinstance(c, Mapping) and c.get("id") == expected["id"]
               and re.fullmatch(pattern, str(c.get("command") or "")) for c in checks):
            return True
    return False


def _finding_line(value):
    """Read both structured findings and older JSON-encoded finding strings."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return compact(value, 300)
        if not isinstance(parsed, Mapping):
            return compact(value, 300)
        value = parsed
    if not isinstance(value, Mapping):
        return ""
    observation = compact(value.get("observation"), 260)
    interpretation = compact(value.get("interpretation"), 160)
    section = compact(value.get("section"), 45)
    source = compact(value.get("source_id"), 35)
    evidence = compact(value.get("evidence_id"), 48)
    parts = [observation]
    if interpretation:
        parts.append("Interpretation: " + interpretation)
    citation = " / ".join(part for part in (source, evidence, section) if part)
    if citation:
        parts.append("[" + citation + "]")
    return " ".join(part for part in parts if part)


def _research_answer(summary, passages):
    lines = []
    if statement := compact(summary.get("summary"), 340):
        lines.append("  Worker answer: " + statement)
    findings = summary.get("findings")
    findings = findings if isinstance(findings, list) else []
    for finding in findings[:3]:
        if text := _finding_line(finding):
            lines.append("  Worker-reported finding: " + text)
    if not findings:
        for passage in passages[:2]:
            excerpt = compact(passage.get("passage_excerpt"), 260)
            if excerpt:
                lines.append("  Retrieved excerpt: " + excerpt + " ["
                             + compact(passage.get("title"), 70) + " / "
                             + compact(passage.get("section"), 45) + "]")
    return lines


def _research_lines(summary):
    def records(value):
        return [row for row in value[:24] if isinstance(row, Mapping)] if isinstance(value, list) else []

    progress = summary.get("research_progress")
    progress = progress if isinstance(progress, Mapping) else {}
    errors = records(progress.get("acquisition_errors"))
    searches = records(summary.get("search_log"))
    passages = records(progress.get("verified_passages")) + records(summary.get("research_citations"))
    activity = any(progress.get(key) for key in (
        "searches", "passage_scans", "acquisitions", "passages", "failed_acquisitions",
        "blocked_operations", "pdf_coverage",
    ))
    if not (activity or errors or searches or passages):
        return []
    pages = {}
    for row in passages:
        title = compact(row.get("title"), 78)
        source = compact(row.get("source_id"), 30)
        key = (title, source, compact(row.get("evidence_id"), 48))
        sections = row.get("sections_read")
        sections = sections[:8] if isinstance(sections, list) else []
        for location in [row.get("section"), *sections]:
            section = compact(location, 35)
            if title and section and section not in pages.setdefault(key, []):
                pages[key].append(section)
    lines = _research_answer(summary, passages)
    if pages:
        refs = [f"{title} ({', '.join(sections[:4])})"
                for (title, _, _), sections in list(pages.items())[:2]]
        lines.append("  Retained passage receipts: " + "; ".join(refs))
    else:
        lines.append("  No page or section receipts in the retained preview.")
    if errors:
        row = next((r for r in errors if r.get("error") == "response_too_large"), errors[0])
        limit = row.get("limit_bytes")
        size = f" (download limit {limit / 1048576:g} MiB)" if type(limit) is int and 0 < limit <= 2**40 else ""
        lines.append("  Acquisition blocked: " + compact(row.get("title"), 100)
                     + " — " + compact(row.get("error"), 60) + size)
    else:
        # Earlier clients sometimes retained the blocker only in worker prose.
        findings = summary.get("findings")
        if isinstance(findings, list):
            blocker = next((row for row in findings[:6] if isinstance(row, str)
                            and row.startswith("ACQUISITION FAILURE:")), "")
            if blocker:
                lines.append("  Worker-reported acquisition blocker: "
                             + compact(blocker.removeprefix("ACQUISITION FAILURE:"), 185))
    failures = []
    for row in searches:
        errors = row.get("errors")
        status = compact(row.get("status"), 40)
        full_text_error = compact(row.get("full_text_error"), 38)
        if not (errors or full_text_error or status in {
            "source_error", "source_unhealthy", "partial_results", "query_rejected", "read_recommended",
        }):
            continue
        details = [compact(v, 38) for v in list(errors.values())[:2]] if isinstance(errors, Mapping) else []
        if full_text_error:
            details.append(full_text_error)
        detail = ", ".join(dict.fromkeys(filter(None, details)))
        failures.append(f"{compact(row.get('query'), 48)} [{detail or status}]")
    if failures:
        lines.append("  Search coverage incomplete: " + "; ".join(failures[:3]))
    elif not searches:
        lines.append("  Search status log unavailable in the retained preview.")
    return lines


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
    diagnostics = result.get("model_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    usage = diagnostics.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    response_details = []
    if isinstance(diagnostics.get("finish_reason"), str):
        response_details.append("finish_reason=" + compact(diagnostics["finish_reason"], 60))
    for key, value in (("completion_tokens", usage.get("completion_tokens")), ("max_tokens", diagnostics.get("max_tokens"))):
        if type(value) is int:
            response_details.append(f"{key}={value}")
    character = result.get("character_report")
    character = character if isinstance(character, Mapping) else {}
    # Generated first-person interpretation is a report, never evidence.
    character_view = None
    if character.get("version") == "character-activity-v1":
        statement = compact(character.get("summary"), 650)
        reflection = compact(character.get("reflection"), 650)
        threads = _items(character.get("open_threads"), 3, 180)
        if statement and reflection:
            character_view = {"summary": statement, "reflection": reflection,
                              "open_threads": threads}
    return {
        "character_report": character_view,
        "research_lines": _research_lines(summary),
        "model_response": "; ".join(response_details),
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
