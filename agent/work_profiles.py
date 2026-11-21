"""Operator-owned presets for reusable workspace capabilities.

A profile selects authority and execution limits; the character supplies each task.
Reports establish what was reported, never universal proof of task completion.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from .work_admission import ProfileIn, RESEARCH_SEARCH, WORKSPACE_TASK

REPORT_PATH = 'aios-work-report-{request_id}.json'
LEGACY_REPORT_PATH = 'aios-experiment-{request_id}.json'
TASK_DESCRIPTION = 'Inspect, edit, or run checks in the workspace.'
TASK_REPORT_GUIDANCE = (
    '\nExecution reporting requirement: write the expected JSON report file with '
    'objective (string), outcome (completed, partial, or blocked), evidence (list), '
    'and limitations (list). Include a substantive summary (string), artifacts (actual output paths), '
    'and requested_content (list of objects with label and text) when the task asks for file text or other content. '
    'Return these same fields in your final JSON response so the originating character receives the result. '
    'Describe the actual result and concrete checks you ran. '
    'Do not claim success for untested behavior. The configured report check verifies '
    'report structure only, not whether the requested objective succeeded.'
)


def workspace_task_profile(*, legacy_report=False) -> ProfileIn:
    report_path = LEGACY_REPORT_PATH if legacy_report else REPORT_PATH
    check_id = 'experiment_report' if legacy_report else 'work_report'
    command = (
        'python3 -c "import json; from pathlib import Path; '
        f"r=json.loads(Path('{report_path}').read_text()); "
        "assert isinstance(r,dict); assert isinstance(r.get('objective'),str) and r['objective'].strip(); "
        "assert r.get('outcome') in ('completed','partial','blocked'); "
        "assert isinstance(r.get('evidence'),list); assert isinstance(r.get('limitations'),list)\""
    )
    return ProfileIn(
        capability=WORKSPACE_TASK[0],
        purpose=WORKSPACE_TASK[1],
        description=TASK_DESCRIPTION,
        assignment_template={
            'expected_outputs': [{'path': report_path, 'kind': 'file'}],
            'required_checks': [{'id': check_id, 'command': command, 'timeout_seconds': 15}],
            'budgets': {'model_steps': 60, 'wall_seconds': 1800, 'context_chars': 100000},
        },
    )


def research_search_profile() -> ProfileIn:
    task = workspace_task_profile()
    return task.model_copy(update={
        "capability": RESEARCH_SEARCH[0],
        "purpose": RESEARCH_SEARCH[1],
        "description": "Search authorized sources and report findings.",
    })


def expand_request_template(template: dict, request_id: UUID) -> dict:
    """Expand only the host request UUID; never interpolate character text into checks."""
    value = str(request_id)
    def expand(item):
        if isinstance(item, str):
            return item.replace('{request_id}', value)
        if isinstance(item, list):
            return [expand(v) for v in item]
        if isinstance(item, dict):
            return {k: expand(v) for k, v in item.items()}
        return item
    return expand(deepcopy(template))


INLINE_GUIDANCE = (
    '\nReturn the findings directly in the final JSON response with outcome, summary, evidence, '
    'limitations, artifacts (empty when no files were produced), and requested_content. '
    'Do not create a file just to deliver the report. Report observations and identify anything '
    'not determined. Client audit logs are retained by the host.'
)


def reporting_template(template: dict, *, inline: bool):
    """Replace only the built-in file report and its matching format check."""
    if not inline:
        return deepcopy(template), TASK_REPORT_GUIDANCE
    value = deepcopy(template)
    criteria = ("expected_outputs", "required_checks")
    has_criteria = any(value.get(k) for k in criteria)
    if has_criteria and not any(
        all(value.get(k, []) == standard[k] for k in criteria)
        for standard in (workspace_task_profile().assignment_template,
                         workspace_task_profile(legacy_report=True).assignment_template)
    ):
        raise ValueError("Inline reporting conflicts with custom profile outputs/checks")
    value["expected_outputs"] = []
    value["required_checks"] = []
    return value, INLINE_GUIDANCE
