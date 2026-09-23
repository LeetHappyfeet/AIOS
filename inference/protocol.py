from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


class StructuredResponseError(ValueError):
    pass


@dataclass(frozen=True)
class StructuredActionProposal:
    type: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class StructuredInferenceResponse:
    expression: str
    actions: tuple[StructuredActionProposal, ...]
    raw: dict[str, Any]


def extract_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
        if value.lower().startswith("json\n"):
            value = value[5:].lstrip()
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StructuredResponseError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise StructuredResponseError("response root must be a JSON object")
    return decoded


def validate_structured_response(
    payload: Mapping[str, Any],
    *,
    allowed_actions: Mapping[str, Mapping[str, Any]] | None = None,
) -> StructuredInferenceResponse:
    expression = payload.get("expression", "")
    if not isinstance(expression, str):
        raise StructuredResponseError("expression must be a string")
    raw_actions = payload.get("actions", [])
    if not isinstance(raw_actions, list):
        raise StructuredResponseError("actions must be an array")
    proposals: list[StructuredActionProposal] = []
    allowed = dict(allowed_actions or {})
    for index, item in enumerate(raw_actions):
        if not isinstance(item, dict):
            raise StructuredResponseError(f"actions[{index}] must be an object")
        action_type = item.get("type")
        arguments = item.get("arguments", {})
        if not isinstance(action_type, str) or not action_type.strip():
            raise StructuredResponseError(f"actions[{index}].type must be a non-empty string")
        if not isinstance(arguments, dict):
            raise StructuredResponseError(f"actions[{index}].arguments must be an object")
        if allowed and action_type not in allowed:
            raise StructuredResponseError(f"action '{action_type}' is not allowed for this request")
        proposals.append(StructuredActionProposal(action_type, dict(arguments)))
    return StructuredInferenceResponse(expression, tuple(proposals), dict(payload))
