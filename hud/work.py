"""Readable work receipts with explicit evidence scope and bounded detail."""
from __future__ import annotations

from aios_app.hud.work_results import compact, result_view


def render_work_results(work, tasks):
    lines = []
    requests = work.get("requests") or []
    by_run = {r["run_id"]: r for r in requests if r.get("run_id")}
    visible_runs = {t["run_id"] for t in tasks}
    separate = [r for r in requests if not r.get("run_id") or r["run_id"] not in visible_runs]
    if separate:
        lines.append("WORK REQUESTS (admission/execution status; not goal completion)")
        for request in separate:
            lines.append(f"- Request {request['request_id']}: {request['status']} — {compact(request.get('objective'), 180)}")
            if request.get("run_id"):
                lines.append(f"  Run {request['run_id']}: details omitted from this HUD; do not resubmit.")
            else:
                lines.append("  " + compact(request.get("reason"), 240))
                if request["status"] == "needs_configuration":
                    lines.append("  Still pending; operator must configure or dismiss this request. Do not duplicate it.")
    if work.get("history_count"):
        lines.append(f"WORK HISTORY: {work['history_count']} closed admission requests retained for operator inspection; no retry required.")
    if tasks:
        lines.append("REMOTE WORK (worker-reported evidence; execution is not goal completion)")
        spent = 0
        for index, task in enumerate(tasks):
            request = by_run.get(task["run_id"], {})
            request_id = task.get("request_id") or request.get("request_id")
            title = compact(task.get("objective") or request.get("objective"), 180)
            entry = [f"- {title or 'Workspace task'}",
                     f"  Run: {task['run_id']}" + (f"; request: {request_id}" if request_id else ""),
                     f"  Execution: {task['status']}; goal review: {task.get('goal_review') or 'not linked'}"]
            view = task.get("result_summary")
            if not isinstance(view, dict):
                view = result_view({"report": task.get("summary")}, {})
            if task["status"] in {"queued", "running"}:
                entry.append("  Progress (worker report): " + compact(task.get("summary"), 240))
                if task.get("cancel_requested"):
                    entry.append("  Cancellation requested; execution has not yet reported termination.")
            else:
                entry.append("  Worker-reported outcome: " + view["worker_outcome"])
            if view["error"]:
                entry.append("  Error: " + view["error"])
            for limitation in view["limitations"]:
                entry.append("  Worker-reported limitation: " + limitation)
            if not view["limitations"] and task["status"] not in {"queued", "running"}:
                entry.append("  Limitations: not supplied by worker.")
            for violation in view["violations"]:
                entry.append("  Reported violation: " + violation)
            entry.append("  Configured checks: " + str(task.get("verification_status") or "not reported")
                         + " — " + view["verification_scope"])
            for check in view["checks"]:
                flags = ("; timed out" if check["timed_out"] else "") + ("; cancelled" if check["cancelled"] else "")
                entry.append(f"  Check {check['id']}: exit {check['exit_code'] if check['exit_code'] is not None else 'not reported'}{flags}")
            if view["checks_omitted"]:
                entry.append(f"  … {view['checks_omitted']} additional checks in run details.")
            if view["worker_verification_scope"]:
                entry.append("  Worker-reported verification scope: " + view["worker_verification_scope"])
            if view["context_budget"]:
                entry.append("  Context budget (worker report): " + view["context_budget"])
            for finding in view["findings"]:
                entry.append("  Worker-reported finding: " + finding)
            for artifact in view["artifacts"]:
                entry.append("  Reported artifact: " + artifact)
            if view["summary"]:
                entry.append("  Worker summary: " + view["summary"])
            elif view["report_excerpt"]:
                entry.append("  Worker report excerpt (unverified): " + view["report_excerpt"])
            entry.append("  Evidence: worker-supplied. Full result and submitted check evidence remain in operator request inspection.")
            size = sum(len(line) + 1 for line in entry)
            if spent + size > 12000:
                lines.append(f"… {len(tasks) - index} run entries omitted by HUD budget; omission is not failure or permission to resubmit.")
                break
            lines.extend(entry)
            spent += size
    return lines
