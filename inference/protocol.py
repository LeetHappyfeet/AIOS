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
    if allowed_actions is not None and not allowed_actions:
        # Transactional/classifier calls have no action authority. Preserve
        # their compact response fields instead of requiring expression/actions.
        return StructuredInferenceResponse(expression=expression, actions=(), raw=dict(payload))
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
        if allowed:
            schema = allowed.get(action_type) or {}
            required = schema.get("required", [])
            properties = schema.get("properties", {})
            if not isinstance(required, list) or not isinstance(properties, dict):
                raise StructuredResponseError(f"configured schema for action '{action_type}' is malformed")
            missing = [key for key in required if key not in arguments]
            if missing:
                raise StructuredResponseError(f"action '{action_type}' is missing required arguments: {missing}")
            if schema.get("additionalProperties") is False:
                unknown = [key for key in arguments if key not in properties]
                if unknown:
                    raise StructuredResponseError(f"action '{action_type}' has unknown arguments: {unknown}")
            type_map = {"string": str, "object": dict, "array": list, "boolean": bool, "integer": int, "number": (int, float)}
            for key, value in arguments.items():
                expected = (properties.get(key) or {}).get("type")
                python_type = type_map.get(expected)
                if python_type is not None and not isinstance(value, python_type):
                    raise StructuredResponseError(f"action '{action_type}' argument '{key}' must be {expected}")
        proposals.append(StructuredActionProposal(action_type, dict(arguments)))
    return StructuredInferenceResponse(expression, tuple(proposals), dict(payload))
