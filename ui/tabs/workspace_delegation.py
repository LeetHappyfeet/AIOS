"""Trusted operator control plane for environment pairing and work admission."""
from __future__ import annotations

import asyncio
import json
import threading
from html import escape
from uuid import UUID

import gradio as gr
from fastapi import HTTPException
from pydantic import ValidationError

from aios_app.config import settings
from aios_app.db import Database
from aios_app.agent.workspace_setup import WorkspaceSetup, ExecutionIn
from aios_app.agent.workspace_registrations import AuthorizationIn, RegistrationStore
from aios_app.agent.workspaces import WorkspaceStore
from aios_app.agent.environment_links import EnvironmentStore, LinkIn, MemorySharingIn
from aios_app.agent.work_admission import WorkAdmission, ProfileIn, WorkDecision, WorkDismissal
from aios_app.agent.work_profiles import experimental_profile
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


def discover(character, selected_origin=None):
    rows = run(WorkspaceStore(db).instances(character)) if character else []
    choices = [
        (f"{r['user_name']} / {r['scope_key']} / chat {r['session_id'][:8]} / instance {r['instance_id'][:8]}",
         r['instance_id'])
        for r in rows
    ]
    value = selected_origin if any(key == selected_origin for _, key in choices) else None
    return gr.Dropdown(choices=choices, value=value)


def manual_bindings(origin):
    async def load():
        setup = WorkspaceSetup(db)
        chat = await setup.origin(uid(origin))
        rows = await setup.bindings(chat['character_id'])
        return [row for row in rows if row['user_name'] == chat['user_name']
                and (row['instance_id'] == origin or not row['attached'])]

    rows = run(load()) if origin else []
    choices = [
        (f"{r['worker_key']} / {r['root_hint']} / "
         f"{'this chat' if r['instance_id'] == origin else 'instance ' + r['instance_id'][:8]}",
         r['binding_id'])
        for r in rows
    ]
    return gr.Dropdown(choices=choices, value=None)


def chat_connection(origin, connect=False):
    if not origin:
        return "Select a chat to see its workspace connection.", None, gr.Button(interactive=False)

    async def load():
        instance_id = uid(origin)
        setup = WorkspaceSetup(db)
        report = await setup.readiness(instance_id)
        status = await setup.connection_status(report)
        if connect and status[2]:
            routing_problem = await RegistrationStore(db).ensure_available(instance_id)
            status = await setup.connection_status(await setup.readiness(instance_id), routing_problem)
        return status

    summary, report, can_connect = run(load())
    return summary, report, gr.Button(interactive=can_connect)


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
    if not origin or not binding:
        return gr.Dropdown(choices=[], value=None), None
    report = readiness(origin, binding)
    choices = [(f"{e['link_id']} ({'enabled' if e['enabled'] else 'revoked'})", e['link_id']) for e in report['environments']]
    return gr.Dropdown(choices=choices, value=choices[0][1] if len(choices) == 1 else None), report


def profiles(binding, link_id):
    if not binding or not link_id:
        return gr.Dropdown(choices=[], value=None), None

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
    rows = run(load()) if origin else []
    choices = [
        (f"{r['status'].replace('_', ' ').title()} / {' '.join(r['intent']['objective'].split())[:100]} / {r['request_id'][:8]}",
         r['request_id'])
        for r in rows
    ]
    return gr.Dropdown(choices=choices, value=None), rows


def inspect(origin, request):
    if not request:
        return None
    return run(WorkAdmission(db).inspect(uid(origin), uid(request)))


def decide(origin, request, accept, reviewer, reason):
    return run(WorkAdmission(db).decide(uid(origin), uid(request), WorkDecision(accept=accept, reviewer=reviewer, reason=reason)))


def dismiss(origin, request, reviewer, reason):
    return run(WorkAdmission(db).dismiss(uid(origin), uid(request), WorkDismissal(reviewer=reviewer, reason=reason)))


def enable_experiments(binding, link_id):
    return run(WorkAdmission(db).configure(uid(binding), uid(link_id), experimental_profile()))


APPROVAL_POLICIES = [
    ("Require approval for each request", "require_approval"),
    ("Allow requests under the configured policy", "allow"),
]


def registered_workspaces(selected=None):
    rows = run(RegistrationStore(db).listing())
    choices = [
        (f"{r['character_id']} / {r['user_name']} / {r['scope_key']} / {r['root_hint']} / "
         f"{r['registration_id'][:8]} · "
         f"{'authorized' if r['enabled'] and r['disposition'] != 'deny' else 'authorization needed'} · "
         f"{'online' if r['worker_online'] else 'offline'}", r['registration_id'])
        for r in rows
    ]
    return gr.Dropdown(choices=choices,
                       value=selected if any(r['registration_id'] == selected for r in rows) else None)


def registration_details(registration):
    async def load():
        return await RegistrationStore(db).details(uid(registration))

    if not registration:
        return ("Select a registered workspace.", None, gr.Dropdown(value=None),
                gr.Button(interactive=False), gr.Button(interactive=False),
                gr.Button(interactive=False), False)
    row = run(load())
    authorized = row['enabled'] and row['disposition'] != 'deny'
    status = "Authorized" if authorized else "Authorization needed"
    online = "client online" if row['worker_online'] else "client offline"
    summary = (f"**{status} · {online}**  \n"
               f"{escape(row['character_id'])} / {escape(row['user_name'])} / "
               f"{escape(row['scope_key'])} · {escape(row['root_hint'])}  \n"
               f"Active chat links: {row['active_link_count']}")
    saved_policy = row['disposition'] if row['disposition'] != 'deny' else 'require_approval'
    return (summary, row, gr.Dropdown(value=saved_policy), gr.Button(interactive=not authorized),
            gr.Button(interactive=authorized), gr.Button(interactive=row['enabled']), False)


def authorize_workspace(registration, disposition, action, confirmed=False):
    if action == 'disable' and not confirmed:
        raise gr.Error("Confirm that disabling revokes active links and cancels linked work.")
    async def save():
        store = RegistrationStore(db)
        registration_id = uid(registration)
        async with db.atomic():
            row = await store.get(registration_id, lock=True)
            authorized = row['enabled'] and row['disposition'] != 'deny'
            if action == 'policy' and not authorized:
                raise HTTPException(409, "This workspace is disabled. Refresh workspaces and authorize it first.")
            if action == 'authorize' and authorized:
                raise HTTPException(409, "This workspace is already authorized. Refresh workspaces to edit its policy.")
            policy = row['disposition'] if action == 'disable' else disposition
            if action != 'disable' and policy not in {'require_approval', 'allow'}:
                raise ValueError("Select an approval policy.")
            await store.authorize(registration_id, AuthorizationIn(enabled=action != 'disable', disposition=policy))

    run(save())
    return registered_workspaces(registration)


EXPERIMENT = experimental_profile()
EXPERIMENT_TEMPLATE = EXPERIMENT.assignment_template

CHECKS = json.dumps(EXPERIMENT_TEMPLATE['required_checks'], indent=2)


@register_tab
def render():
    with gr.Tab("Workspace Delegation"):
        gr.Markdown("### Workspaces\n"
                    "In AIOS-Client, select **Automatic chat routing**, register the workspace, and start the listener. "
                    "Then authorize that workspace here.")
        registered_refresh = gr.Button("Refresh workspaces")
        registration = gr.Dropdown(label="Registered workspace", choices=[])
        workspace_summary = gr.Markdown("Select a registered workspace.")
        registered_policy = gr.Dropdown(label="Approval policy", choices=APPROVAL_POLICIES, value=None)
        with gr.Row():
            authorize = gr.Button("Authorize workspace", interactive=False)
            save_approval = gr.Button("Save approval policy", interactive=False)
        with gr.Accordion("Disable workspace", open=False):
            confirm_disable = gr.Checkbox(label="Revoke active chat links and cancel linked work")
            disable = gr.Button("Disable workspace", interactive=False)
        with gr.Accordion("Registration details", open=False):
            registered_report = gr.JSON()

        gr.Markdown("### Chat connections\n"
                    "Select a chat to check its connection. Matching chats attach when AIOS builds their HUD. "
                    "Use **Connect this chat** to attach the selected chat here.")
        refresh_characters = gr.Button("Load characters")
        character = gr.Dropdown(label="Character", choices=[])
        refresh = gr.Button("Refresh chats and connection status")
        origin = gr.Dropdown(label="Chat", choices=[])
        connection_summary = gr.Markdown("Select a chat to see its workspace connection.")
        connect = gr.Button("Connect this chat", interactive=False)
        with gr.Accordion("Connection details", open=False):
            connection_report = gr.JSON()

        with gr.Accordion("Work requests for the selected chat", open=False):
            refresh_pending = gr.Button("Refresh work requests")
            request = gr.Dropdown(label="Work request", choices=[])
            detail = gr.JSON(label="Selected request and pinned criteria")
            reviewer = gr.Textbox(label="Reviewer", value="operator")
            reason = gr.Textbox(label="Decision reason")
            with gr.Row():
                approve = gr.Button("Approve request")
                deny = gr.Button("Deny request")
                dismiss_request = gr.Button("Dismiss unstarted request")
            decision_result = gr.JSON(label="Decision result")
            with gr.Accordion("Recent request details", open=False):
                requests = gr.JSON()

        with gr.Accordion("Advanced workspace setup", open=False):
            gr.Markdown("Use manual pairing for legacy client profiles or a separate execution instance.")
            binding = gr.Dropdown(label="Manual execution binding", choices=[])
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
            refresh_links = gr.Button("Refresh manual links")
            link_id = gr.Dropdown(label="Environment link", choices=[])
            report = gr.JSON(label="Readiness / operation result")
            save_memory = gr.Button("Apply memory sharing setting to selected link")
            revoke = gr.Button("Revoke selected link and cancel its active work")
            gr.Markdown("### General experimental tasks\nEnable `workspace / experimental_task` once on the selected link. "
                        "The character can then request different bounded tasks by changing objective, including inspection, coding and workspace changes. "
                        "The preset allows 60 model steps, 30 minutes and 100,000 context characters. "
                        "Existing execution policies and the client's local permissions still apply. "
                        "Each request writes its own JSON report of outcome, evidence and limitations. "
                        "The host check validates that report's structure; task success still requires task-specific evidence. "
                        "Failed runs are not automatically replayed.")
            enable_general = gr.Button("Enable general experimental tasks on selected link")
            gr.Markdown("### Advanced capability profile\nThe fields below start with experimental-task defaults. "
                        "Profile criteria are immutable after creation; use a new purpose for different criteria. "
                        "Descriptions and enabled status can be edited. `{request_id}` expands to the host request UUID in paths and checks.")
            refresh_profiles = gr.Button("Load profiles on selected link")
            profile = gr.Dropdown(label="Existing profile", choices=[])
            profile_list = gr.JSON(label="Saved profiles")
            capability = gr.Textbox(label="Capability", value=EXPERIMENT.capability)
            purpose = gr.Textbox(label="Purpose", value=EXPERIMENT.purpose)
            description = gr.Textbox(label="Description", value=EXPERIMENT.description)
            enabled = gr.Checkbox(label="Profile enabled", value=True)
            checks = gr.Textbox(label="Required host checks (JSON array)", value=CHECKS, lines=6)
            outputs = gr.Textbox(label="Expected outputs (JSON array)", value=json.dumps(EXPERIMENT_TEMPLATE['expected_outputs']))
            preserve = gr.Textbox(label="Paths to preserve (JSON array)", value="[]")
            with gr.Row():
                steps = gr.Number(label="Maximum model steps", value=60, precision=0)
                seconds = gr.Number(label="Wall time seconds", value=1800, precision=0)
                context = gr.Number(label="Context characters", value=100000, precision=0)
            save = gr.Button("Save capability profile")
            gr.Markdown("### Execution policies\nPolicies apply to all workspace work on each selected instance. "
                        "The most restrictive policy wins. An attached workspace uses only the conversation policy. "
                        "Readiness displays the saved policies; changing a dropdown takes effect only when saved.")
            choices = ['require_approval', 'deny', 'allow']
            op = gr.Dropdown(label="Conversation workspace.run policy to save", choices=choices, value='require_approval')
            ep = gr.Dropdown(label="Separate environment policy (ignored for attached workspace)", choices=choices, value='require_approval')
            save_policy = gr.Button("Save execution policy")

        connection_outputs = [connection_summary, connection_report, connect]
        registration_outputs = [workspace_summary, registered_report, registered_policy,
                                authorize, save_approval, disable, confirm_disable]
        registered_refresh.click(registered_workspaces, registration, registration).success(
            registration_details, registration, registration_outputs)
        registration.input(registration_details, registration, registration_outputs)
        authorize.click(lambda r, p: authorize_workspace(r, p, 'authorize'),
                        [registration, registered_policy], registration).success(
            registration_details, registration, registration_outputs).success(
            chat_connection, origin, connection_outputs)
        save_approval.click(lambda r, p: authorize_workspace(r, p, 'policy'),
                            [registration, registered_policy], registration).success(
            registration_details, registration, registration_outputs).success(
            chat_connection, origin, connection_outputs)
        disable.click(lambda r, c: authorize_workspace(r, None, 'disable', c),
                      [registration, confirm_disable], registration).success(
            registration_details, registration, registration_outputs).success(
            chat_connection, origin, connection_outputs)

        refresh_characters.click(characters, outputs=character)
        character.change(discover, character, origin)
        refresh.click(discover, [character, origin], origin).success(
            chat_connection, origin, connection_outputs).success(manual_bindings, origin, binding)
        origin.change(chat_connection, origin, connection_outputs)
        connect.click(lambda chat: chat_connection(chat, connect=True), origin, connection_outputs).success(
            manual_bindings, origin, binding)
        origin.change(manual_bindings, origin, binding)
        origin.change(execution_choices, origin, execution)
        execution.change(execution_coordinates, execution, coordinates)
        create.click(create_execution, [origin, label], coordinates)
        make_link.click(link, [origin, binding, share], [link_id, report]).success(
            chat_connection, origin, connection_outputs)
        binding.change(links, [origin, binding], [link_id, report])
        refresh_links.click(links, [origin, binding], [link_id, report])
        save_memory.click(lambda b, l, share: run(EnvironmentStore(db).set_memory(uid(b), uid(l), MemorySharingIn(share_memory=share))), [binding, link_id, share], report)
        revoke.click(lambda b, l: run(EnvironmentStore(db).revoke(uid(b), uid(l))), [binding, link_id], report).success(
            chat_connection, origin, connection_outputs)
        link_id.change(profiles, [binding, link_id], [profile, profile_list])
        refresh_profiles.click(profiles, [binding, link_id], [profile, profile_list])
        fields = [capability, purpose, description, enabled, checks, outputs, preserve, steps, seconds, context]
        profile.change(load_profile, profile, fields)
        save.click(save_profile, [binding, link_id] + fields, report).success(
            chat_connection, origin, connection_outputs)
        enable_general.click(enable_experiments, [binding, link_id], report).success(
            chat_connection, origin, connection_outputs)
        save_policy.click(policies, [origin, binding, op, ep], report).success(
            chat_connection, origin, connection_outputs)
        origin.change(pending, origin, [request, requests]).success(
            lambda: None, outputs=decision_result)
        refresh_pending.click(pending, origin, [request, requests])
        request.change(inspect, [origin, request], detail)
        decision_inputs = [origin, request, reviewer, reason]
        approve.click(lambda o, r, who, why: decide(o, r, True, who, why),
                      decision_inputs, decision_result).success(pending, origin, [request, requests])
        deny.click(lambda o, r, who, why: decide(o, r, False, who, why),
                   decision_inputs, decision_result).success(pending, origin, [request, requests])
        dismiss_request.click(dismiss, decision_inputs, decision_result).success(pending, origin, [request, requests])
