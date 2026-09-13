from __future__ import annotations

import json
from typing import Any, Iterable, Optional


class CausalRuleViolation(ValueError):
    pass


def _rule_data(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return dict(value)


def validate_action_rules(
    rules: Iterable[dict[str, Any]],
    *,
    action_type: str,
    actor_entity_type: Optional[str],
    target_entity_type: Optional[str],
    has_target: bool,
) -> None:
    """Single implementation of legacy world_rule action constraints.

    This function is deterministic policy only.  It does not inspect character
    belief/knowledge tables and it never changes epistemic state.
    """

    for rule in rules:
        data = _rule_data(rule.get("rule_data"))
        rule_type = str(rule.get("rule_type") or "constraint")
        rule_key = str(rule.get("rule_key") or "unnamed")

        if rule_type == "action_allowlist":
            actions = set(data.get("actions") or [])
            if actions and action_type not in actions:
                raise CausalRuleViolation(
                    f"world rule '{rule_key}' does not allow action '{action_type}'"
                )

        elif rule_type == "require_target":
            applies = data.get("action_type", "*")
            if applies in ("*", action_type) and not has_target:
                raise CausalRuleViolation(
                    f"world rule '{rule_key}' requires a target for '{action_type}'"
                )

        elif rule_type == "require_target_type":
            applies = data.get("action_type", "*")
            allowed_types = set(data.get("target_entity_types") or [])
            if applies in ("*", action_type) and allowed_types:
                if target_entity_type not in allowed_types:
                    raise CausalRuleViolation(
                        f"world rule '{rule_key}' rejects target type "
                        f"'{target_entity_type}' for '{action_type}'"
                    )

        elif rule_type == "deny_action":
            applies = data.get("action_type", "*")
            if applies not in ("*", action_type):
                continue
            actor_filter = data.get("actor_entity_type")
            target_filter = data.get("target_entity_type")
            if actor_filter and actor_filter != actor_entity_type:
                continue
            if target_filter and target_filter != target_entity_type:
                continue
            raise CausalRuleViolation(
                f"world rule '{rule_key}' denies action '{action_type}'"
            )
