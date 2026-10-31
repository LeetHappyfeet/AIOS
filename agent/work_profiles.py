"""Operator-owned presets for reusable workspace capabilities.

A profile selects authority and execution limits; the character supplies each task.
Reports establish what was reported, never universal proof of task completion.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from .work_admission import ProfileIn

EXPERIMENT_CAPABILITY = 'workspace'
EXPERIMENT_PURPOSE = 'experimental_task'
REPORT_PATH = 'aios-experiment-{request_id}.json'
EXPERIMENT_GUIDANCE = (
    '\nExecution reporting requirement: write the expected JSON report file with '
    'objective (string), outcome (completed, partial, or blocked), evidence (list), '
    'and limitations (list). Include a substantive summary (string), artifacts (actual output paths), '
    'and requested_content (list of objects with label and text) when the task asks for file text or other content. '
    'Return these same fields in your final JSON response so the originating character receives the result. '
    'Describe the actual result and concrete checks you ran. '
    'Do not claim success for untested behavior. The configured report check verifies '
    'report structure only, not whether the requested objective succeeded.'
)


def experimental_profile() -> ProfileIn:
    command = (
        'python3 -c "import json; from pathlib import Path; '
        f"r=json.loads(Path('{REPORT_PATH}').read_text()); "
        "assert isinstance(r,dict); assert isinstance(r.get('objective'),str) and r['objective'].strip(); "
        "assert r.get('outcome') in ('completed','partial','blocked'); "
        "assert isinstance(r.get('evidence'),list); assert isinstance(r.get('limitations'),list)\""
    )
    return ProfileIn(
        capability=EXPERIMENT_CAPABILITY,
        purpose=EXPERIMENT_PURPOSE,
        description='Perform a bounded experimental task in the authorized workspace. Put the task in objective; report actual outcome, evidence and limitations in JSON. Report checks do not prove task success.',
        assignment_template={
            'expected_outputs': [{'path': REPORT_PATH, 'kind': 'file'}],
            'required_checks': [{'id': 'experiment_report', 'command': command, 'timeout_seconds': 15}],
            'budgets': {'model_steps': 60, 'wall_seconds': 1800, 'context_chars': 100000},
        },
    )


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


def reporting_template(template: dict, *, read_only: bool):
    """Resolve the built-in file-report convention before the assignment is sealed.

    Only the exact built-in output/check pair can be replaced. Custom operator
    checks must never be silently bypassed by character-supplied constraints.
    """
    if not read_only:
        return deepcopy(template), EXPERIMENT_GUIDANCE
    value = deepcopy(template)
    standard = experimental_profile().assignment_template
    criteria = ("expected_outputs", "required_checks")
    has_criteria = any(value.get(k) for k in criteria)
    if has_criteria and any(value.get(k, []) != standard.get(k, []) for k in criteria):
        raise ValueError("Read-only inspection conflicts with custom profile outputs/checks; configure a profile without file outputs or shell checks")
    value["expected_outputs"] = []
    value["required_checks"] = []
    return value, (
        '\nRead-only inspection: use inspect_environment and permitted file reads. '
        'Do not create a report file or run shell commands. Return the findings directly '
        'in the final JSON response with outcome, summary, evidence, limitations, artifacts '
        '(empty when no files were produced), and requested_content. '
        'Report observations and explicitly identify anything not determined. '
        'Client audit logs are retained by the host outside the task output contract.'
    )
