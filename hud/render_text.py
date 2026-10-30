from __future__ import annotations

import json
from typing import Any, Mapping

from aios_app.hud.recent_events import normalized_text, project_recent_events


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
        rendered = f"{value} / {maximum}" if minimum is not None and maximum is not None else str(value)
    else:
        rendered = str(value)
    if unit:
        rendered = f"{rendered} {unit}"
    return f"{label}: {rendered}"


def _knowledge_annotation(item: Mapping[str, Any]) -> str:
    anchor = item.get("anchor") or {}
    if not anchor:
        return ""
    relationship = str(anchor.get("relationship") or "references").replace("_", " ")
    target_label = anchor.get("target_label")
    visible = bool(anchor.get("world_visible"))
    bits = [relationship]
    if target_label:
        bits.append(f"about {target_label}")
    if visible:
        labels = []
        for entry in item.get("world_context") or []:
            label = entry.get("label")
            if not label or label == target_label or label in labels:
                continue
            labels.append(str(label))
            if len(labels) >= 3:
                break
        if labels:
            bits.append("context: " + ", ".join(labels))
    else:
        bits.append("anchored context outside visible world; neighbors withheld")
    return " [" + "; ".join(bits) + "]"


def _epistemic_label(item: Mapping[str, Any]) -> str:
    state = str(item.get("authority_state") or "").strip()
    origin = str(item.get("origin_kind") or "").strip().replace("_", "-")
    mode = str(item.get("epistemic_mode") or "").strip().replace("_", "-")
    bits = [value for value in (state, origin, mode) if value]
    return (" [" + " | ".join(bits) + "]") if bits else ""


def _content_status(item: Mapping[str, Any]) -> str:
    """A source-observation is not direct observation of derived content."""
    status = str(item.get("epistemic_status") or "known").strip()
    origin = str(item.get("origin_kind") or "").strip().replace("-", "_").lower()
    mode = str(item.get("epistemic_mode") or "").strip().replace("-", "_").lower()
    if origin in {"model_inference", "user_testimony", "character_testimony"} or mode in {
        "inference", "testimony", "reported", "derived"
    }:
        if status.lower() in {"observed", "known"}:
            return "reported/derived; underlying event not independently observed"
    return status


def _scene_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("text") or value.get("label") or "").strip()
    return str(value or "").strip()


def _render_goal_plan(plan: Mapping[str, Any] | None) -> list[str]:
    if not plan:
        return []
    proposal = plan.get("proposal") or {}
    if not proposal:
        return ["Planning pending."]
    lines = ([] if plan.get("context_current", True) else ["Earlier advisory plan; context has changed."])
    lines += ["Tentative approach (not evidence of progress): " + str(proposal.get("strategy") or "")]
    for step in proposal.get("steps") or []:
        lines.append("Proposed step: " + str(step))
    for question in proposal.get("open_questions") or []:
        lines.append(f"Unknown ({question['kind']}): {question['question']}")
    lines.append("Next: " + str(proposal.get("next") or "wait").replace("_", " "))
    continuation = plan.get("continuation") or {}
    if continuation.get("status"):
        lines.append("Lookup: " + str(continuation["status"]) + " — " + str(continuation.get("reason") or ""))
    for dependency in continuation.get("dependencies") or []:
        status = str(dependency.get("status") or "pending").replace("_", " ")
        lines.append(f"Lookup ({status}): {dependency.get('question', '')}")
        if dependency.get("status") == "awaiting_observation":
            lines.append("Option: inspect or ask about this prerequisite; its answer is not established.")
        elif dependency.get("status") == "unavailable":
            lines.append("Lookup temporarily unavailable; background retry pending.")
    return lines


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
    stats = [f"{key}={state[key]}" for key in ("health", "stamina", "energy") if state.get(key) is not None]
    if stats:
        lines.append("State: " + "  ".join(stats))
    physical = state.get("physical") or {}
    emotional = state.get("emotional") or {}
    if physical:
        lines.append("Physical: " + ", ".join(f"{k}={v}" for k, v in physical.items()))
    if emotional:
        lines.append("Emotion: " + ", ".join(f"{k}={v}" for k, v in emotional.items()))
    identity_kernel = identity.get("identity_kernel")
    if identity_kernel:
        lines.append("\n" + str(identity_kernel))
    for section in frame.get("plugin_sections") or []:
        title = str(section.get("title") or section.get("key") or "PLUGIN")
        fields = section.get("fields") or []
        if fields:
            lines.append(f"\n{title.upper()}:")
            for field in fields:
                lines.append("- " + _render_plugin_field(field))
    catalog = frame.get("entity_catalog")
    if catalog is not None:
        from aios_app.hud.entity_catalog import TITLE, NOTICE

        lines.append("\n" + TITLE)
        if catalog.get("status") != "ready":
            lines.append("- Catalog unavailable; locations and entities remain unknown.")
        elif catalog.get("entries"):
            lines.append(NOTICE)
            lines.extend("- " + entry["text"] for entry in catalog["entries"])
        else:
            lines.append("- No references selected within this view/budget; this does not establish absence.")
    actors = [] if catalog is not None else scene.get("actors") or []
    objects = [] if catalog is not None else scene.get("objects") or []
    if actors:
        lines.append("\nPRESENT PEOPLE:")
        for actor in actors:
            lines.append(f"- {_name(actor)}")
    if objects:
        lines.append("\nPRESENT OBJECTS:")
        for obj in objects:
            lines.append(f"- {_name(obj)}")
    working_scene = scene.get("working_state") or {}
    scene_lines = []
    immediate_goal = working_scene.get("immediate_goal")
    pending_work = working_scene.get("pending_work") or working_scene.get("pending_action")
    last_change = working_scene.get("last_significant_change")
    if _scene_text(immediate_goal):
        scene_lines.append(f"Immediate managed goal: {_scene_text(immediate_goal)[:180]}")
        if isinstance(immediate_goal, Mapping):
            scene_lines.extend(_render_goal_plan(immediate_goal.get("plan")))
            for requirement in (immediate_goal.get("knowledge_requirements") or [])[:2]:
                status=str(requirement.get("coverage_status") or "unassessed")
                count=len(requirement.get("dossier_ids") or [])
                scene_lines.append(
                    f"Goal knowledge: {status}; linked research dossiers: {count}")
    if _scene_text(pending_work):
        scene_lines.append(f"Pending work: {_scene_text(pending_work)}")
    # A pre-patch scene can still contain a copied DAG turn. Suppress it.
    if (not isinstance(last_change, Mapping) or
            last_change.get("source") != "dag_node"):
        if _scene_text(last_change):
            scene_lines.append(f"Last scene change: {_scene_text(last_change)[:220]}")
    if scene_lines:
        lines.append("\nCURRENT SCENE:")
        lines.extend(f"- {value}" for value in scene_lines)
    research = frame.get("research_activity") or {}
    if research:
        status=str(research.get("status") or "requested")
        outcome=str(research.get("outcome") or "").strip()
        detail=f" ({outcome})" if outcome and outcome != status else ""
        lines.append("\nRESEARCH ACTIVITY:")
        lines.append(f"- {status}{detail}: {str(research.get('question') or '')[:180]}")
        if status=="completed":
            lines.append(
                f"- Authorized references found: {int(research.get('source_count') or 0)}"
                " (not acquired knowledge)")
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
            provenance = _content_status(item)
            qualifier = f" [{provenance}]" if provenance.startswith("reported/derived") else ""
            lines.append(f"-{qualifier}{_epistemic_label(item)}{_knowledge_annotation(item)} {item.get('text', '')}")
    retrieval_evidence = frame.get("retrieval_evidence") or {}
    if retrieval_evidence.get("route") == "character":
        status = str(retrieval_evidence.get("status") or "").strip().lower()
        demand_kind = str(retrieval_evidence.get("demand_kind") or "memory").replace("_", " ")
        if status == "established":
            memory_status = f"Established character evidence found for this {demand_kind} question."
        elif status == "partial":
            memory_status = (
                f"Related character memory found, but the requested {demand_kind} is not established."
            )
        elif status == "unestablished":
            memory_status = f"No established character memory answers this {demand_kind} question."
        else:
            memory_status = ""
        if memory_status:
            lines.append("\nMEMORY STATUS:")
            lines.append(f"- {memory_status}")

    beliefs = frame.get("beliefs") or []
    if beliefs:
        lines.append("\nKNOWLEDGE / BELIEFS:")
        for item in beliefs:
            status = _content_status(item)
            confidence = item.get("effective_confidence")
            suffix = f" confidence={confidence:.2f}" if isinstance(confidence, (float, int)) else ""
            lines.append(f"- [{status}{suffix}]{_epistemic_label(item)}{_knowledge_annotation(item)} {item.get('text', '')}")
            for conflict in (item.get("conflicts") or [])[:3]:
                lines.append(f"  ! conflicts with: {conflict.get('text', '')}")
    rdf_evidence = frame.get("rdf_evidence") or []
    if rdf_evidence:
        lines.append("\nRECALLED SOURCE REPORTS (ATTRIBUTED; NOT VERIFIED CURRENT WORLD STATE):")
        for item in rdf_evidence[:3]:
            provenance = item.get("provenance") or {}
            speaker = provenance.get("speaker_id") or "source"
            lines.append(f"- [{speaker}; earlier report] {item.get('text', '')}")

    corpus_diagnostics = (frame.get("hud") or {}).get("corpus_research") or {}
    lookup = corpus_diagnostics.get("demand") or {}
    if lookup.get("lookup_status") and lookup["lookup_status"] != "not_requested":
        lines.append("\nREFERENCE LOOKUP: " + str(lookup["lookup_status"]).replace("_", " "))
        if corpus_diagnostics.get("budget_excluded_count"):
            lines.append("Some retrieved references were omitted by the HUD budget.")
    corpus_references = frame.get("corpus_references") or []
    if corpus_references:
        lines.append("\nCORPUS REFERENCES (LOOKED UP; NOT MEMORY OR BELIEF):")
        lines.append("External references describe their source setting; they do not establish personal experiences, relationships, possessions, or goal completion in this instance.")
        lines.append("Option: review a relevant reference and check its requirements before attempting it.")
        for item in corpus_references:
            source_bits = [
                str(value)
                for value in (item.get("title"), item.get("heading"))
                if value
            ]
            source = " / ".join(source_bits)
            prefix = f"[{source}] " if source else ""
            attribution = ""
            if item.get("section_id"):
                attribution = (f" [source={item['section_id']}; namespace={item.get('epistemic_namespace', 'reference')}; "
                               f"binding={item.get('identity_binding', 'external')}; "
                               f"chars={item.get('passage_start', 0)}:{item.get('passage_end', 0)}]")
            lines.append(f"- {prefix}{item.get('text', '')}{attribution}")

    goals = frame.get("goals") or []
    immediate_goal_id = (
        str(immediate_goal.get("goal_id") or "") if isinstance(immediate_goal, Mapping) else ""
    )
    immediate_goal_key = normalized_text(_scene_text(immediate_goal))
    rendered_goals = []
    for goal in goals:
        text = goal.get("text") if isinstance(goal, dict) else str(goal)
        goal_id = str(goal.get("goal_id") or "") if isinstance(goal, dict) else ""
        # Goal identity is authoritative; normalized text remains compatibility
        # fallback for legacy scene snapshots without goal_id.
        if immediate_goal_id and goal_id == immediate_goal_id:
            continue
        if not immediate_goal_id and immediate_goal_key and normalized_text(str(text)) == immediate_goal_key:
            continue
        rendered_goals.append((goal, text))
    if rendered_goals:
        lines.append("\nOTHER ACTIVE GOALS:")
        for goal, text in rendered_goals:
            annotation = _knowledge_annotation(goal) if isinstance(goal, dict) else ""
            requirements=(goal.get("knowledge_requirements") or []) if isinstance(goal, Mapping) else []
            demand=f" [knowledge: {requirements[0].get('coverage_status', 'unassessed')}]" if requirements else ""
            lines.append(f"-{annotation} {text}{demand}")
            if isinstance(goal, Mapping):
                lines.extend("  " + entry for entry in _render_goal_plan(goal.get("plan")))
    scheduled_goals = frame.get("scheduled_goals") or []
    if scheduled_goals:
        lines.append("\nFUTURE GOALS (SCHEDULED FOR REVIEW):")
        for goal in scheduled_goals:
            text = goal.get("text") if isinstance(goal, dict) else str(goal)
            expression = goal.get("time_expression") if isinstance(goal, dict) else None
            timezone_name = goal.get("timezone") if isinstance(goal, dict) else None
            timing = str(expression or goal.get("due_at") or "") if isinstance(goal, dict) else ""
            if timezone_name and expression:
                timing += f" ({timezone_name})"
            lines.append(f"- {text}" + (f" — review {timing}" if timing else ""))
    rules = frame.get("rules") or []
    if rules:
        lines.append("\nWORLD RULES:")
        for rule in rules:
            text = rule.get("text") or rule.get("rule_key") or str(rule)
            lines.append(f"-{_knowledge_annotation(rule)} {text}")

    # Recent events are episodic context, not a speaker transcript. Project
    # semantic events into narrative form, collapse exact duplicates, and prefer
    # a bounded semantic projection over the raw source turn when both point at
    # the same source node.
    recent = project_recent_events(frame.get("recent_events") or [])
    if recent:
        lines.append("\nRECENT EVENTS:")
        for event in recent:
            rendered_text = event.get("text") or event.get("message_text")
            if not rendered_text:
                continue
            if event.get("message_text"):
                stream = event.get("event_stream") or "runtime"
                role = event.get("speaker_role") or "other"
                speaker = event.get("speaker_id") or "unknown"
                if role=="character":
                    # Preserve the complete source in SQL; show the tool
                    # request separately under RESEARCH ACTIVITY in the HUD.
                    from aios_app.agent.research_action import strip_tool_markup
                    rendered_text=strip_tool_markup(rendered_text)
                if rendered_text:
                    lines.append(f"- [{stream}|{role}:{speaker}] {rendered_text}")
            else:
                lines.append(f"- {rendered_text}")
    lines.append("\nStay inside this HUD's epistemic and branch boundaries.")
    work = frame.get("work") or {}
    capabilities = work.get("capabilities") or []
    if capabilities:
        lines.append("WORK CAPABILITIES (request intent; AIOS decides authorization and routing)")
        for capability in capabilities:
            description = " ".join(str(capability["description"]).split())[:300]
            lines.append(f"- {capability['capability']} / {capability['purpose']}: {description}; worker {'recently seen' if capability['online'] else 'not recently seen'}")
        example = next((p for p in capabilities if (p['capability'], p['purpose']) == ('workspace', 'experimental_task')), capabilities[0])
        action = {"op": "request_work", "capability": example['capability'], "purpose": example['purpose'],
                  "objective": "Describe the concrete task and desired result here", "constraints": []}
        lines.append('Copy capability and purpose exactly from the list; do not invent names. Put the concrete task in objective. Budgets, authority and host checks are configured by the operator.')
        if (example['capability'], example['purpose']) == ('workspace', 'experimental_task'):
            lines.append('Different experimental tasks reuse workspace / experimental_task; no new capability or purpose is needed for each objective.')
        lines.append('To request work, use native request_work when registered. Otherwise emit exactly one complete structured block in your reply, outside Markdown code fences. Example with an available pair: <aios_action>' + json.dumps(action, separators=(',', ':')) + '</aios_action>. Replace objective with the actual bounded task. AIOS returns admission/status in a later HUD. Never claim execution from a request, or resubmit work already pending.')
    elif work.get("unavailable_reason"):
        lines.append("WORK UNAVAILABLE: " + str(work["unavailable_reason"])[:300])
    if work.get("request_error"):
        lines.append("WORK REQUEST NEEDS CLARIFICATION: " + str(work["request_error"]["reason"])[:300])
    if work.get("requests"):
        lines.append("WORK REQUESTS (admission/execution status; not goal completion)")
        for request in work["requests"]:
            reason = " ".join(str(request.get("reason") or "").split())[:240]
            objective = " ".join(str(request.get("objective") or "").split())[:160]
            lines.append(f"- {request['request_id']}: {request['status']} — {objective}")
            lines.append(f"  {reason}")
    tasks = frame.get("remote_tasks") or []
    if tasks:
        lines.append("REMOTE WORK (reported task state; not character speech)")
        for task in tasks:
            summary = " ".join(str(task.get("summary") or "").split())[:320]
            lines.append(f"- {task['run_id']}: {task['status']} — {summary}")
            if task.get("verification_status"):
                lines.append(f"  Checks: {task['verification_status']}; goal review: {task.get('goal_review') or 'not linked'}")
    return "\n".join(lines)
