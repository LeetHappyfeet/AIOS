"""Real SQL contracts for capability admission, policy and origin provenance."""

import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import test_workspace_client as workspace_tests
from test_character_environments import pair
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aios_app.agent.work_admission import (
    WorkAdmission,
    WorkIntent,
    WorkRequestIn,
    ProfileIn,
    WorkDecision,
    extract_work_intent,
)
from aios_app.agent.workspaces import WorkspaceStore, install_workspace_routes
from aios_app.hud.render_text import render_hud_text

db = workspace_tests.db


@pytest.fixture(autouse=True)
def scene(monkeypatch):
    from aios_app.agent.lifecycle import CharacterAgencyStore

    monkeypatch.setattr(CharacterAgencyStore, "_refresh_scene", AsyncMock())


async def setup(db, *, allow=True, online=True):
    store, origin, execution, bid, lid = await pair(db, share=False)
    await db.execute(
        "CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY,viewpoint_id text,superseded_at timestamptz)"
    )
    await db.execute(
        "ALTER TABLE aios.dag_node ADD COLUMN speaker_role text,ADD COLUMN speaker_id text,ADD COLUMN message_text text"
    )
    await db.execute("INSERT INTO aios.ingest_event VALUES(1,'Cortana',NULL)")
    source = await db.fetchval(
        "SELECT source_head_node_id FROM aios.character_runtime_state WHERE instance_id=$1",
        origin.instance_id,
    )
    await db.execute(
        "UPDATE aios.dag_node SET speaker_role='character',speaker_id='Cortana' WHERE node_id=$1",
        source,
    )
    work = WorkAdmission(db)
    profile = await work.configure(
        bid,
        lid,
        ProfileIn(
            capability="software_development",
            purpose="inspect_and_repair",
            description="Inspect and repair the authorized project",
            assignment_template={
                "required_checks": [{"id": "unit", "command": "python -m unittest -v"}],
                "preserve_paths": ["tests.py"],
            },
        ),
    )
    if allow:
        for instance in (origin.instance_id, execution.instance_id):
            await db.execute(
                "INSERT INTO aios.character_action_policy VALUES($1,'workspace.run','allow')",
                instance,
            )
    if online:
        await db.execute(
            "UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1",
            bid,
        )
    req = WorkRequestIn(
        request_id=uuid4(),
        source_node_id=source,
        capability="software_development",
        purpose="inspect_and_repair",
        objective="Fix calculator addition",
        constraints=["Retain the public interface"],
    )
    return work, origin, execution, bid, lid, source, req, profile


def test_intent_never_accepts_destination_identity_or_checks():
    raw = dict(capability="software_development", purpose="repair", objective="Fix add")
    for extra in (
        {"binding_id": str(uuid4())},
        {"goal_id": str(uuid4())},
        {"required_checks": []},
        {"command": "rm -rf /"},
    ):
        with pytest.raises(ValidationError):
            WorkIntent.model_validate({**raw, **extra})
    assert (
        extract_work_intent(
            '<aios_action>{"op":"research","question":"x"}</aios_action>'
        )
        is None
    )
    with pytest.raises(ValueError):
        extract_work_intent(
            "<aios_action>{}</aios_action><aios_action>{}</aios_action>"
        )
    with pytest.raises(ValidationError):
        ProfileIn(
            capability="code",
            purpose="repair",
            description="Repair",
            assignment_template={},
        )


@pytest.mark.asyncio
async def test_request_routes_by_capability_and_pins_operator_checks(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    result = await work.request(origin.instance_id, req)
    assert result["status"] == "queued" and result["completed"] is False
    run = await db.fetchrow(
        "SELECT * FROM aios.workspace_run WHERE run_id=$1", UUID(result["run_id"])
    )
    assert run["origin_instance_id"] == origin.instance_id
    assert run["binding_id"] == bid and run["required_protocol_version"] == 3
    assignment = (
        json.loads(run["assignment"])
        if isinstance(run["assignment"], str)
        else run["assignment"]
    )
    assert assignment["required_checks"][0]["command"] == "python -m unittest -v"
    assert assignment["preserve_paths"] == ["tests.py"]
    assert await work.request(origin.instance_id, req) == result
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1
    assert await WorkspaceStore(db).claim(bid, "worker", 2) is None
    assert (await WorkspaceStore(db).claim(bid, "worker", 3))["run_id"] == result[
        "run_id"
    ]
    overlay = await work.overlay(origin.instance_id, run["origin_source_timeline_id"])
    rendered = render_hud_text({"work": overlay})
    assert "software_development / inspect_and_repair" in rendered
    assert "request_work" in rendered and "running" in rendered
    assert str(bid) not in rendered and str(lid) not in rendered


@pytest.mark.asyncio
async def test_default_policy_waits_for_separate_operator_decision(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(
        db, allow=False
    )
    result = await work.request(origin.instance_id, req)
    assert result["status"] == "awaiting_approval"
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 0
    decision = WorkDecision(
        accept=True, reviewer="human", reason="Approved bounded calculator task"
    )
    accepted = await work.decide(origin.instance_id, req.request_id, decision)
    assert accepted["status"] == "queued"
    assert await work.decide(origin.instance_id, req.request_id, decision) == accepted
    with pytest.raises(HTTPException):
        await work.decide(
            origin.instance_id,
            req.request_id,
            decision.model_copy(update={"accept": False}),
        )


@pytest.mark.asyncio
async def test_deny_cannot_be_overridden_by_request_or_hidden_as_available(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    await db.execute(
        "UPDATE aios.character_action_policy SET disposition='deny' WHERE instance_id=$1",
        execution.instance_id,
    )
    result = await work.request(origin.instance_id, req)
    assert result["status"] == "denied"
    with pytest.raises(HTTPException):
        await work.decide(
            origin.instance_id,
            req.request_id,
            WorkDecision(accept=True, reviewer="operator", reason="try"),
        )
    assert not (
        await work.overlay(
            origin.instance_id,
            (await work.source(origin.instance_id, source))["source_timeline_id"],
        )
    )["capabilities"]


@pytest.mark.asyncio
async def test_waiting_client_reconsiders_without_duplicate_runs(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(
        db, online=False
    )
    assert (await work.request(origin.instance_id, req))["status"] == "waiting_client"
    await db.execute(
        "UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1", bid
    )
    await work.reconsider(binding_id=bid)
    await work.reconsider(binding_id=bid)
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1
    # Same intent on a later ordinary chat turn does not launch concurrent duplicate work.
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    later = uuid4()
    await db.execute(
        "INSERT INTO aios.dag_node(node_id,timeline_id,event_id) VALUES($1,$2,1)",
        later,
        timeline,
    )
    await db.execute(
        "INSERT INTO aios.dag_edge VALUES($1,$2,$3)", timeline, later, source
    )
    await db.execute(
        "UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1",
        origin.instance_id,
        later,
    )
    original = await db.fetchval("SELECT run_id FROM aios.workspace_run")
    retry = await work.request(
        origin.instance_id,
        req.model_copy(update={"source_node_id": later, "request_id": uuid4()}),
    )
    assert retry["run_id"] == str(original)
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1


@pytest.mark.asyncio
async def test_source_markup_requires_character_and_not_foreign_or_superseded_source(
    db,
):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    text = (
        "<aios_action>"
        + json.dumps({"op": "request_work", **req.intent().model_dump()})
        + "</aios_action>"
    )
    await db.execute(
        "UPDATE aios.dag_node SET message_text=$2,speaker_role='user' WHERE node_id=$1",
        source,
        text,
    )
    with pytest.raises(HTTPException):
        await work.from_source(origin.instance_id, source)
    await db.execute(
        "UPDATE aios.dag_node SET speaker_role='character' WHERE node_id=$1", source
    )
    assert (await work.from_source(origin.instance_id, source))["status"] == "queued"
    assert (await work.from_source(origin.instance_id, source))["status"] == "queued"
    with pytest.raises(HTTPException):
        await work.request(execution.instance_id, req)
    with pytest.raises(HTTPException):
        await work.request(
            origin.instance_id,
            req.model_copy(
                update={
                    "objective": "Different objective",
                    "request_id": UUID(
                        (await work.from_source(origin.instance_id, source))[
                            "request_id"
                        ]
                    ),
                }
            ),
        )
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1


@pytest.mark.asyncio
async def test_pending_goal_attempt_is_not_replaced_by_newer_goal(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(
        db, online=False
    )
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    gid, aid = uuid4(), uuid4()
    await db.execute(
        "INSERT INTO aios.character_agent_goal(goal_id,instance_id,status,goal_text) VALUES($1,$2,'active','Repair calculator')",
        gid,
        origin.instance_id,
    )
    await db.execute(
        "INSERT INTO aios.character_goal_attempt VALUES($1,$2,$3,$4,NULL)",
        aid,
        gid,
        origin.instance_id,
        timeline,
    )
    assert (await work.request(origin.instance_id, req))["status"] == "waiting_client"
    await db.execute(
        "UPDATE aios.character_goal_attempt SET closed_at=now() WHERE attempt_id=$1",
        aid,
    )
    await db.execute(
        "UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1", bid
    )
    await work.reconsider(binding_id=bid)
    assert (
        await db.fetchval("SELECT status FROM aios.character_work_request")
        == "invalidated"
    )
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 0


@pytest.mark.asyncio
async def test_profile_criteria_and_source_are_immutable(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(
        db, allow=False
    )
    await work.request(origin.instance_id, req)
    with pytest.raises(Exception, match="immutable"):
        async with db.atomic():
            await db.execute(
                "UPDATE aios.workspace_work_profile SET assignment_template='{}'"
            )
    with pytest.raises(Exception, match="immutable"):
        async with db.atomic():
            await db.execute("UPDATE aios.character_work_request SET intent='{}'")


def test_native_request_has_distinct_credentials_and_parses_json_uuids(monkeypatch):
    import aios_app.agent.workspaces as routes

    monkeypatch.setenv("AIOS_WORKSPACE_API_KEY", "worker")
    monkeypatch.setenv("AIOS_WORKSPACE_REVIEW_KEY", "operator")
    monkeypatch.setenv("AIOS_WORK_REQUEST_API_KEY", "integration")
    mock = AsyncMock(return_value={"status": "awaiting_approval"})
    monkeypatch.setattr(routes.WorkAdmission, "request", mock)
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    path = f"/agent/instance/{uuid4()}/work/request"
    body = {
        "request_id": str(uuid4()),
        "source_node_id": str(uuid4()),
        "capability": "software_development",
        "purpose": "inspect_and_repair",
        "objective": "Fix calculator",
    }
    with TestClient(app) as client:
        assert (
            client.post(
                path, json=body, headers={"Authorization": "Bearer worker"}
            ).status_code
            == 401
        )
        response = client.post(
            path, json=body, headers={"Authorization": "Bearer integration"}
        )
        assert response.status_code == 200, response.text
        assert isinstance(mock.call_args.args[1].source_node_id, UUID)


@pytest.mark.asyncio
async def test_malformed_source_receipt_is_visible_without_launching_work(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    await db.execute(
        "UPDATE aios.dag_node SET message_text=$2 WHERE node_id=$1",
        source,
        '<aios_action>{"op":"request_work","capability":"software_development","command":"do anything"}</aios_action>',
    )
    result = await work.from_source(origin.instance_id, source)
    assert result["status"] == "needs_clarification"
    assert await work.from_source(origin.instance_id, source) == result
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    overlay = await work.overlay(origin.instance_id, timeline)
    assert "NEEDS CLARIFICATION" in render_hud_text({"work": overlay})
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 0


@pytest.mark.asyncio
async def test_alias_retry_keeps_original_receipt_after_execution_finishes(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    first = await work.request(origin.instance_id, req)
    later = uuid4()
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    await db.execute(
        "INSERT INTO aios.dag_node(node_id,timeline_id,event_id) VALUES($1,$2,1)",
        later,
        timeline,
    )
    await db.execute(
        "INSERT INTO aios.dag_edge VALUES($1,$2,$3)", timeline, later, source
    )
    await db.execute(
        "UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1",
        origin.instance_id,
        later,
    )
    other = req.model_copy(update={"source_node_id": later, "request_id": uuid4()})
    assert (await work.request(origin.instance_id, other))["run_id"] == first["run_id"]
    await db.execute(
        "UPDATE aios.workspace_run SET status='finished' WHERE run_id=$1",
        UUID(first["run_id"]),
    )
    assert (await work.request(origin.instance_id, other))["run_id"] == first["run_id"]
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1


@pytest.mark.asyncio
async def test_internal_action_and_source_transport_use_same_admission(db):
    from aios_app.agent.actions import default_action_registry

    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    action = default_action_registry(db).get("work.request")
    assert action is not None
    result = await action.handler(
        origin.instance_id, {**req.intent().model_dump(), "_worker_class": "planning"}
    )
    assert result["status"] == "queued"
    assert (await work.request(origin.instance_id, req))["run_id"] == result["run_id"]
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1


@pytest.mark.asyncio
async def test_old_branch_request_cannot_start_when_worker_returns(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(
        db, online=False
    )
    assert (await work.request(origin.instance_id, req))["status"] == "waiting_client"
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    fork = uuid4()
    await db.execute(
        "INSERT INTO aios.dag_node(node_id,timeline_id,event_id) VALUES($1,$2,1)",
        fork,
        timeline,
    )
    await db.execute(
        "UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1",
        origin.instance_id,
        fork,
    )
    await db.execute(
        "UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1", bid
    )
    await work.reconsider(binding_id=bid)
    assert (
        await db.fetchval("SELECT status FROM aios.character_work_request")
        == "invalidated"
    )
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 0


@pytest.mark.asyncio
async def test_unknown_capability_never_falls_back_to_arbitrary_machine(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    result = await work.request(
        origin.instance_id, req.model_copy(update={"capability": "android_control"})
    )
    assert result["status"] == "needs_configuration"
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 0


@pytest.mark.asyncio
async def test_simultaneous_native_requests_launch_once_on_postgres(db):
    import os
    import asyncio

    if not os.environ.get("AIOS_WORKSPACE_TEST_DSN"):
        pytest.skip("Native PostgreSQL pool required for concurrency test")
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    results = await asyncio.gather(
        work.request(origin.instance_id, req), work.request(origin.instance_id, req)
    )
    assert results[0]["run_id"] == results[1]["run_id"]
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1


@pytest.mark.asyncio
async def test_normal_ingestion_relays_only_new_character_work_after_adoption(
    monkeypatch,
):
    from test_ingest_replay_pipeline import FakeDB, request, install_structural_mocks
    from aios_app import ingest_api
    from aios_app.agent.admission import AutonomyAdmissionService
    import aios_app.agent.work_admission as module

    instance = uuid4()
    db = FakeDB(
        {"event_id": 42, "inserted": True, "was_superseded": False}, [None, None]
    )
    _, node, calls = install_structural_mocks(monkeypatch, instance_ids=[instance])
    monkeypatch.setattr(
        ingest_api, "ensure_participant", AsyncMock(return_value=uuid4())
    )
    monkeypatch.setattr(ingest_api, "bind_available_instance", AsyncMock())
    monkeypatch.setattr(ingest_api, "record_message_participation", AsyncMock())
    monkeypatch.setattr(
        AutonomyAdmissionService, "observe_host_experience", AsyncMock()
    )
    relay = AsyncMock()
    monkeypatch.setattr(module, "ingest_work_request", relay)
    text = '<aios_action>{"op":"request_work","capability":"code","purpose":"repair","objective":"Fix calculator"}</aios_action>'
    out = await ingest_api.ingest_message(db, request(text=text))
    assert out.disposition == "new" and calls["dirty"]
    relay.assert_awaited_once_with(db, [instance], node)
    relay.reset_mock()
    replay = FakeDB(
        {"event_id": 42, "inserted": False, "was_superseded": False},
        [
            {"node_id": node, "timeline_id": out.timeline_id},
            {"source_head_node_id": node},
        ],
    )
    await ingest_api.ingest_message(replay, request(text=text))
    relay.assert_not_awaited()


def test_long_work_markup_is_not_rendered_as_character_narrative():
    from aios_app.agent.research_action import strip_tool_markup

    raw = {
        "op": "request_work",
        "capability": "code",
        "purpose": "repair",
        "objective": "x" * 3900,
        "constraints": ["Keep interface" * 10],
    }
    text = (
        "I will request the repair. <aios_action>" + json.dumps(raw) + "</aios_action>"
    )
    assert extract_work_intent(text) is not None
    assert strip_tool_markup(text) == "I will request the repair."


@pytest.mark.parametrize(
    "wrap",
    [lambda x: "```xml\n" + x + "\n```", lambda x: "> " + x, lambda x: "`" + x + "`"],
)
def test_quoted_action_examples_do_not_request_execution(wrap):
    block = '<aios_action>{"op":"request_work","capability":"code","purpose":"repair","objective":"Fix sums"}</aios_action>'
    assert extract_work_intent(wrap(block)) is None


@pytest.mark.asyncio
async def test_operator_inspection_exposes_pinned_checks_and_selected_binding(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(
        db, allow=False
    )
    await work.request(origin.instance_id, req)
    info = await work.inspect(origin.instance_id, req.request_id)
    assert info["execution_binding_id"] == str(bid)
    assert (
        info["profile_snapshot"]["required_checks"][0]["command"]
        == "python -m unittest -v"
    )
    with pytest.raises(HTTPException):
        await work.inspect(execution.instance_id, req.request_id)


@pytest.mark.asyncio
async def test_existing_sillytavern_action_relay_returns_work_receipt(db):
    from aios_app.agent.api import install_external_agency_routes, ResearchToolRequestIn

    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    text = (
        "<aios_action>"
        + json.dumps({"op": "request_work", **req.intent().model_dump()})
        + "</aios_action>"
    )
    await db.execute(
        "UPDATE aios.dag_node SET message_text=$2 WHERE node_id=$1", source, text
    )
    admitted = await work.from_source(origin.instance_id, source)
    app = FastAPI()
    install_external_agency_routes(app, db)
    endpoint = next(
        r.endpoint
        for r in app.routes
        if getattr(r, "path", "")
        == "/agent/instance/{instance_id}/research/tool-request"
    )
    result = await endpoint(
        origin.instance_id, ResearchToolRequestIn(source_node_id=source)
    )
    assert (
        result["operation"] == "request_work" and result["run_id"] == admitted["run_id"]
    )
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1
