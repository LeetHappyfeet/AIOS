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
