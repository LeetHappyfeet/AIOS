"""Trusted operator control plane for environment pairing and work admission."""
from __future__ import annotations

import asyncio
import json
import threading
from uuid import UUID

import gradio as gr
from fastapi import HTTPException
from pydantic import ValidationError

from aios_app.config import settings
from aios_app.db import Database
from aios_app.agent.workspace_setup import WorkspaceSetup, ExecutionIn
from aios_app.agent.workspaces import WorkspaceStore
from aios_app.agent.environment_links import EnvironmentStore, LinkIn, MemorySharingIn
from aios_app.agent.work_admission import WorkAdmission, ProfileIn, WorkDecision
from aios_app.agent.policy import ActionPolicyService
from aios_app.ui.registry import register_tab

_loop = asyncio.new_event_loop()
_connect_lock = asyncio.Lock()
threading.Thread(target=_loop.run_forever, daemon=True).start()
db = Database(settings.db_dsn)


def run(coro):
    async def connected():
        async with _connect_lock:
            if db.pool is None:
                await db.connect()
        return await coro
    try:
        return asyncio.run_coroutine_threadsafe(connected(), _loop).result()
    except (HTTPException, ValidationError, ValueError) as exc:
        raise gr.Error(str(exc.detail) if isinstance(exc, HTTPException) else str(exc)) from exc


def uid(value):
    if not value:
        raise gr.Error("Select the required coordinate first, then refresh discovery if needed")
    return UUID(value)


def characters():
    rows = run(WorkspaceSetup(db).characters())
    return gr.Dropdown(choices=[(r['display_name'] or r['character_id'], r['character_id']) for r in rows], value=None)


def discover(character):
    if not character:
        return gr.Dropdown(choices=[], value=None), gr.Dropdown(choices=[], value=None)
    instances = run(WorkspaceStore(db).instances(character))
    bindings = run(WorkspaceSetup(db).bindings(character))
    origins = [(f"{r['user_name']} / {r['scope_key']} / session {r['session_id']} / {r['instance_id']}", r['instance_id']) for r in instances]
    workers = [(f"{b['workspace_key']} / {b['worker_key']} / {b['user_name']} / {b['root_hint']}", b['binding_id']) for b in bindings]
    return gr.Dropdown(choices=origins, value=None), gr.Dropdown(choices=workers, value=None)


def execution_choices(origin):
    async def load():
        o = await WorkspaceSetup(db).origin(uid(origin))
        rows = await WorkspaceStore(db).instances(o['character_id'])
        return [r for r in rows if r['user_name'] == o['user_name']
                and r['instance_id'] != str(o['instance_id']) and r['world_id'] != str(o['world_id'])
                and r['source_timeline_id'] and r['source_timeline_id'] != str(o['source_timeline_id'])]
    if not origin:
        return gr.Dropdown(choices=[], value=None)
    rows = run(load())
    return gr.Dropdown(choices=[(f"session {r['session_id']} / {r['scope_key']} / {r['instance_id']}", r['instance_id']) for r in rows], value=None)


def execution_coordinates(instance):
    from aios_app.agent.workspaces import public
    async def load():
        return public(await WorkspaceSetup(db).origin(uid(instance)))
    return run(load()) if instance else None


def create_execution(origin, label):
    return run(WorkspaceSetup(db).create_execution(ExecutionIn(origin_instance_id=uid(origin), label=label)))


def readiness(origin, binding):
    return run(WorkspaceSetup(db).readiness(uid(origin), uid(binding) if binding else None))


def link(origin, binding, share):
    result = run(EnvironmentStore(db).create(uid(binding), LinkIn(origin_instance_id=uid(origin), share_memory=share)))
    return gr.Dropdown(choices=[(result['link_id'], result['link_id'])], value=result['link_id']), result


def links(origin, binding):
    report = readiness(origin, binding)
    choices = [(f"{e['link_id']} ({'enabled' if e['enabled'] else 'revoked'})", e['link_id']) for e in report['environments']]
    return gr.Dropdown(choices=choices, value=None), report


def profiles(binding, link_id):
    async def load():
        await EnvironmentStore(db).link(uid(binding), uid(link_id), enabled=False)
        from aios_app.agent.workspaces import public
        rows = await db.fetch("SELECT * FROM aios.workspace_work_profile WHERE link_id=$1 ORDER BY capability,purpose", uid(link_id))
        return [public(r) for r in rows]
    rows = run(load())
    return gr.Dropdown(choices=[(f"{r['capability']} / {r['purpose']}", r['profile_id']) for r in rows], value=None), rows


def load_profile(profile_id):
    if not profile_id:
        return (gr.skip(),) * 10
    async def load():
        from aios_app.agent.workspaces import public
        r = await db.fetchrow("SELECT * FROM aios.workspace_work_profile WHERE profile_id=$1", uid(profile_id))
        if not r:
            raise ValueError("Profile no longer exists")
        return public(r)
    p = run(load())
    t = p['assignment_template']
    return (p['capability'], p['purpose'], p['description'], p['enabled'],
            json.dumps(t.get('required_checks', []), indent=2), json.dumps(t.get('expected_outputs', []), indent=2),
            json.dumps(t.get('preserve_paths', [])), t['budgets']['model_steps'], t['budgets']['wall_seconds'], t['budgets']['context_chars'])


def save_profile(binding, link_id, capability, purpose, description, enabled, checks, outputs, preserve, steps, seconds, context):
    try:
        template = {"required_checks": json.loads(checks), "expected_outputs": json.loads(outputs),
                    "preserve_paths": json.loads(preserve), "budgets": {"model_steps": steps, "wall_seconds": seconds, "context_chars": context}}
        req = ProfileIn(capability=capability, purpose=purpose, description=description, enabled=enabled, assignment_template=template)
        return run(WorkAdmission(db).configure(uid(binding), uid(link_id), req))
    except (ValueError, ValidationError) as exc:
        raise gr.Error(str(exc)) from exc


def policies(origin, binding, origin_policy, execution_policy):
    async def save():
        async with db.atomic():
            b = await WorkspaceStore(db).binding(uid(binding), lock=True)
            o = await WorkspaceSetup(db).origin(uid(origin))
            if b['character_id'] != o['character_id'] or b['user_name'] != o['user_name']:
                raise ValueError("Select a binding for the same character and human user")
            selected = {uid(origin): origin_policy}
            if b['instance_id'] != uid(origin):
                selected[b['instance_id']] = execution_policy
            for instance, disposition in selected.items():
                await ActionPolicyService(db).set_policy(instance, 'workspace.run', disposition)
        return await WorkspaceSetup(db).readiness(uid(origin), uid(binding))
    return run(save())


def pending(origin):
    async def load():
        from aios_app.agent.workspaces import public
        rows = await db.fetch("SELECT * FROM aios.character_work_request WHERE instance_id=$1 ORDER BY created_at DESC LIMIT 100", uid(origin))
        return [public(r) for r in rows]
    rows = run(load())
    return gr.Dropdown(choices=[(f"{r['status']} / {r['request_id']}", r['request_id']) for r in rows], value=None), rows


def inspect(origin, request):
    if not request:
        return None
    return run(WorkAdmission(db).inspect(uid(origin), uid(request)))


def decide(origin, request, accept, reviewer, reason):
    return run(WorkAdmission(db).decide(uid(origin), uid(request), WorkDecision(accept=accept, reviewer=reviewer, reason=reason)))


CHECKS = json.dumps([{"id": "hello_contents", "command": "python3 -c \"from pathlib import Path; assert Path('aios-hello.txt').read_text().strip() == 'Hello from AIOS'\"", "cwd": ".", "timeout_seconds": 10}], indent=2)


@register_tab
def render():
    with gr.Tab("Workspace Delegation"):
        gr.Markdown("### Attach a workspace to a conversation\n"
                    "Select the existing conversation and its client binding, then click Attach/link selected workspace. "
                    "A client already bound to that conversation can be used directly. No additional character instance is needed. "
                    "Work evidence is stored separately from the chat source; ordinary chat turns do not interrupt the listener. "
                    "Connection success is separate from delegation readiness: configure a profile and check readiness below.")
        refresh_characters = gr.Button("Discover characters")
        character = gr.Dropdown(label="Character", choices=[])
        refresh = gr.Button("Refresh instances and client bindings")
        origin = gr.Dropdown(label="Conversational origin (human / scope / session / instance)", choices=[])
        binding = gr.Dropdown(label="Execution binding (workspace / worker / human / root)", choices=[])
        with gr.Accordion("Optional: use a separate execution environment", open=False):
            execution = gr.Dropdown(label="Existing separate execution instance for this human", choices=[])
            label = gr.Textbox(label="New execution session label", value="Execution workspace")
            create = gr.Button("Create separate execution instance")
            coordinates = gr.JSON(label="Execution coordinates for client profile")
            gr.Markdown("For an independent environment, create or select an instance and bind a separate client profile to it. "
                        "The ordinary attachment flow above uses your existing conversation and binding.")
        gr.Markdown("For an attached workspace, leave memory sharing unchecked: it already reads this instance's memory. "
                    "Memory sharing below applies only between separate environments.")
        share = gr.Checkbox(label="Share eligible character memory between these environments", value=False)
        make_link = gr.Button("Attach/link selected workspace")
        refresh_links = gr.Button("Load links and readiness")
        link_id = gr.Dropdown(label="Environment link", choices=[])
        report = gr.JSON(label="Readiness / operation result")
        check = gr.Button("Check delegation readiness")
        save_memory = gr.Button("Apply memory sharing setting to selected link")
        revoke = gr.Button("Revoke selected link and cancel its active work")
        gr.Markdown("### Capability profile\nDefault values define a harmless first test. Ask the character to use "
                    "`workspace_test / write_hello` to write `aios-hello.txt` containing `Hello from AIOS`. "
                    "The host checks the file after execution. Profile criteria are immutable after creation; use a new purpose for different criteria. "
                    "Descriptions and enabled status can be edited.")
        refresh_profiles = gr.Button("Load profiles on selected link")
        profile = gr.Dropdown(label="Existing profile", choices=[])
        profile_list = gr.JSON(label="Saved profiles")
        capability = gr.Textbox(label="Capability", value="workspace_test")
        purpose = gr.Textbox(label="Purpose", value="write_hello")
        description = gr.Textbox(label="Description", value="Write and verify a harmless greeting file in the authorized workspace")
        enabled = gr.Checkbox(label="Profile enabled", value=True)
        checks = gr.Textbox(label="Required host checks (JSON array)", value=CHECKS, lines=6)
        outputs = gr.Textbox(label="Expected outputs (JSON array)", value='[{"path":"aios-hello.txt","kind":"file"}]')
        preserve = gr.Textbox(label="Paths to preserve (JSON array)", value="[]")
        with gr.Row():
            steps = gr.Number(label="Maximum model steps", value=5, precision=0)
            seconds = gr.Number(label="Wall time seconds", value=120, precision=0)
            context = gr.Number(label="Context characters", value=10000, precision=0)
        save = gr.Button("Save capability profile")
        gr.Markdown("### Execution policies\nPolicies apply to all workspace work on each selected instance. "
                    "The most restrictive policy wins. An attached workspace uses only the conversation policy. "
                    "Readiness displays the saved policies; changing a dropdown takes effect only when saved.")
        choices = ['require_approval', 'deny', 'allow']
        op = gr.Dropdown(label="Conversation workspace.run policy to save", choices=choices, value='require_approval')
        ep = gr.Dropdown(label="Separate environment policy (ignored for attached workspace)", choices=choices, value='require_approval')
        save_policy = gr.Button("Save execution policy")
        gr.Markdown("### Admission and pending approvals\nRefresh after the character emits a work request. Inspect its objective and pinned "
                    "profile/checks before deciding. Approval authorizes that request; it does not mark work complete. "
                    "After the listener executes and verifies, the next conversational HUD reports the run status and verification result.")
        refresh_pending = gr.Button("Refresh work requests")
        request = gr.Dropdown(label="Work request", choices=[])
        requests = gr.JSON(label="Recent work requests")
        detail = gr.JSON(label="Selected request and pinned criteria")
        accept = gr.Checkbox(label="Approve (unchecked means deny)", value=False)
        reviewer = gr.Textbox(label="Reviewer", value="operator")
        reason = gr.Textbox(label="Decision reason")
        decision = gr.Button("Record request decision")
        refresh_characters.click(characters, outputs=character)
        character.change(discover, character, [origin, binding])
        refresh.click(discover, character, [origin, binding])
        origin.change(execution_choices, origin, execution)
        execution.change(execution_coordinates, execution, coordinates)
        create.click(create_execution, [origin, label], coordinates)
        make_link.click(link, [origin, binding, share], [link_id, report])
        refresh_links.click(links, [origin, binding], [link_id, report])
        check.click(readiness, [origin, binding], report)
        save_memory.click(lambda b, l, share: run(EnvironmentStore(db).set_memory(uid(b), uid(l), MemorySharingIn(share_memory=share))), [binding, link_id, share], report)
        revoke.click(lambda b, l: run(EnvironmentStore(db).revoke(uid(b), uid(l))), [binding, link_id], report)
        refresh_profiles.click(profiles, [binding, link_id], [profile, profile_list])
        fields = [capability, purpose, description, enabled, checks, outputs, preserve, steps, seconds, context]
        profile.change(load_profile, profile, fields)
        save.click(save_profile, [binding, link_id] + fields, report)
        save_policy.click(policies, [origin, binding, op, ep], report)
        refresh_pending.click(pending, origin, [request, requests])
        request.change(inspect, [origin, request], detail)
        decision.click(decide, [origin, request, accept, reviewer, reason], report)
