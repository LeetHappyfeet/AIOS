from __future__ import annotations

from typing import Any, Mapping


def _name(value: Mapping[str, Any]) -> str:
    return str(
        value.get("display_name")
        or value.get("canonical_name")
        or value.get("entity_key")
        or value.get("character_id")
        or value.get("entity_id")
        or "unknown"
    )


def _render_plugin_field(field: Mapping[str, Any]) -> str:
    label = str(field.get("label") or field.get("key") or "value")
    value = field.get("value")
    field_type = str(field.get("field_type") or "text")
    unit = field.get("unit")

    if field_type == "gauge":
        minimum = field.get("minimum")
        maximum = field.get("maximum")
        if minimum is not None and maximum is not None:
            rendered = f"{value} / {maximum}"
        else:
            rendered = str(value)
    else:
        rendered = str(value)

    if unit:
        rendered = f"{rendered} {unit}"
    return f"{label}: {rendered}"


def render_hud_text(frame: Mapping[str, Any]) -> str:
    """Deterministically render the canonical HUD JSON into an LLM-facing surface."""

    identity = frame.get("identity") or {}
    presence = frame.get("presence") or {}
    state = frame.get("state") or {}
    scene = frame.get("scene") or {}

    lines = [
        f"=== AIOS HUD: {_name(identity)} ===",
        f"World: {presence.get('world_key', 'unknown')}",
        f"Instance: {presence.get('instance_id', 'unknown')}",
        f"State version: {presence.get('state_version', 'unknown')}",
    ]

    location = scene.get("location")
    if location:
        lines.append(f"Location: {_name(location)}")
    elif presence.get("location_entity_id"):
        lines.append(f"Location entity: {presence['location_entity_id']}")

    stats = [
        f"{key}={state[key]}"
        for key in ("health", "stamina", "energy")
        if state.get(key) is not None
    ]
    if stats:
        lines.append("State: " + "  ".join(stats))

    physical = state.get("physical") or {}
    emotional = state.get("emotional") or {}
    if physical:
        lines.append("Physical: " + ", ".join(f"{k}={v}" for k, v in physical.items()))
    if emotional:
        lines.append("Emotion: " + ", ".join(f"{k}={v}" for k, v in emotional.items()))

    plugin_sections = frame.get("plugin_sections") or []
    for section in plugin_sections:
        title = str(section.get("title") or section.get("key") or "PLUGIN")
        fields = section.get("fields") or []
        if not fields:
            continue
        lines.append(f"\n{title.upper()}:")
        for field in fields:
            lines.append("- " + _render_plugin_field(field))

    actors = scene.get("actors") or []
    objects = scene.get("objects") or []
    if actors:
        lines.append("\nPRESENT PEOPLE:")
        for actor in actors:
            lines.append(f"- {_name(actor)}")
    if objects:
        lines.append("\nPRESENT OBJECTS:")
        for obj in objects:
            lines.append(f"- {_name(obj)}")

    relationships = frame.get("relationships") or []
    if relationships:
        lines.append("\nRELATIONSHIPS:")
        for rel in relationships:
            bits = [_name(rel)]
            if rel.get("relationship_type"):
                bits.append(str(rel["relationship_type"]))
            if rel.get("trust") is not None:
                bits.append(f"trust={rel['trust']}")
            if rel.get("familiarity") is not None:
                bits.append(f"familiarity={rel['familiarity']}")
            lines.append("- " + " | ".join(bits))

    inventory = frame.get("inventory") or []
    if inventory:
        lines.append("\nINVENTORY:")
        for item in inventory:
            equipped = " [equipped]" if item.get("equipped") else ""
            lines.append(f"- {_name(item)} x{item.get('quantity', 1)}{equipped}")

    memories = frame.get("memories") or []
    if memories:
        lines.append("\nACTIVE MEMORY:")
        for item in memories:
            lines.append(f"- {item.get('text', '')}")

    beliefs = frame.get("beliefs") or []
    if beliefs:
        lines.append("\nKNOWLEDGE / BELIEFS:")
        for item in beliefs:
            status = item.get("epistemic_status") or "known"
            confidence = item.get("effective_confidence")
            suffix = f" confidence={confidence:.2f}" if isinstance(confidence, (float, int)) else ""
            lines.append(f"- [{status}{suffix}] {item.get('text', '')}")
            for conflict in item.get("conflicts") or []:
                lines.append(f"  ! conflicts with: {conflict.get('text', '')}")

    goals = frame.get("goals") or []
    if goals:
        lines.append("\nGOALS:")
        for goal in goals:
            text = goal.get("text") if isinstance(goal, dict) else str(goal)
            lines.append(f"- {text}")

    rules = frame.get("rules") or []
    if rules:
        lines.append("\nWORLD RULES:")
        for rule in rules:
            text = rule.get("text") or rule.get("rule_key") or str(rule)
            lines.append(f"- {text}")

    recent = frame.get("recent_events") or []
    if recent:
        lines.append("\nRECENT EVENTS:")
        for event in recent:
            if event.get("message_text"):
                stream = event.get("event_stream") or "runtime"
                role = event.get("speaker_role") or "other"
                speaker = event.get("speaker_id") or "unknown"
                lines.append(f"- [{stream}|{role}:{speaker}] {event['message_text']}")
            elif event.get("text"):
                lines.append(f"- {event['text']}")

    actions = frame.get("actions") or []
    if actions:
        lines.append("\nAVAILABLE ACTIONS: " + ", ".join(str(action) for action in actions))

    lines.append("\nStay inside this HUD's epistemic and branch boundaries.")
    return "\n".join(lines)
