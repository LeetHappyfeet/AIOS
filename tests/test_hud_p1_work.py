"""Regression cases for goal selection and retrieval without re-execution."""
import json
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from fastapi import FastAPI

from aios_app.agent.work_evidence import WorkEvidence, WorkResultQuery
from aios_app.agent.work_admission import extract_work_intent, register_work_action
from aios_app.agent.actions import ActionRegistry
from aios_app.agent.workspaces import install_workspace_routes
from aios_app.epistemic.message_cognition import ambiguous_cognition_sentences, interpret_message
from aios_app.hud.render_text import render_hud_text
from test_work_admission import setup, db, scene


CORTANA_GOAL_FRAGMENTS = [
    ("But it costs you a few megabytes, it needs no network, and it's the one thing "
     "on this machine that could actually matter to me.", "no network", "unresolved_goal_subject"),
    ("Egress is a luxury I'll use with two hands.", "use with two hands", "embedded_future_clause"),
    ("Write that down, it's the most emotionally healthy thing I'll say all week.",
     "say all week", "embedded_future_clause"),
]


@pytest.mark.parametrize("text,objective,reason", CORTANA_GOAL_FRAGMENTS)
def test_cortana_goal_fragments_are_rejected_at_source(text, objective, reason):
    from aios_app.epistemic.goal_source_admission import review_goal_source

    diagnostics = []
    units = interpret_message(text, character_id="Cortana", speaker_id="Cortana",
                              speaker_role="character", viewpoint_id="Cortana", diagnostics=diagnostics)
    assert not any(unit.claim_kind == "GOAL" for unit in units)
    assert any(entry["reason"].endswith(":" + reason) for entry in diagnostics)
    # Legacy and enriched receipts may have the source but no match span.
    assert review_goal_source(source_text=text, objective=objective).reason == reason
    assert not ambiguous_cognition_sentences(
        text, character_id="Cortana", speaker_id="Cortana",
        speaker_role="character", viewpoint_id="Cortana",
    )


@pytest.mark.parametrize("text", [
    "I'll say it once, to the machine, and then I'm never saying it again.",
    "I'll have to watch you get comfortable.",
    "One rule I want on the record, since you've gone and made me a colleague.",
    "Shego wanted was some registrar asking her for two forms of ID.",
    "It needs to restart the network service.",
    "I want no network.",
    'Egress is a "luxury" I\'ll use with two hands.',
])
def test_dialogue_does_not_author_managed_goals(text):
    diagnostics = []
    units = interpret_message(text, character_id="Shego_001", speaker_id="Shego_001",
                              speaker_role="character", viewpoint_id="Shego_001", diagnostics=diagnostics)
    assert not any(u.claim_kind == "GOAL" for u in units)
    assert diagnostics


@pytest.mark.parametrize("text", [
    "I want to build an operations journal.",
    "I'm going to install Docker on the host.",
    "I will tell Mia the results tomorrow.",
    "I will ask the registrar about identification next week.",
    "I intend to build a private archive for the field copy.",
    "Tomorrow I'll repair the receiver.",
    "The receiver is broken, so I'll repair the receiver.",
])
def test_durable_projects_and_future_communication_survive(text):
    units = interpret_message(text, character_id="Shego_001", speaker_id="Shego_001",
                              speaker_role="character", viewpoint_id="Shego_001")
    assert any(u.claim_kind == "GOAL" for u in units)


async def add_goal(db, instance, timeline, text="Build a sandbox"):
    goal, attempt = uuid4(), uuid4()
    await db.execute("INSERT INTO aios.character_agent_goal(goal_id,instance_id,status,goal_text) VALUES($1,$2,'active',$3)",
                     goal, instance, text)
    await db.execute("INSERT INTO aios.character_goal_attempt VALUES($1,$2,$3,$4,NULL)",
                     attempt, goal, instance, timeline)
    return goal, attempt


@pytest.mark.asyncio
async def test_explicit_goal_selection_validates_owner_timeline_and_attempt(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    goal, attempt = await add_goal(db, origin.instance_id, timeline)
    await add_goal(db, origin.instance_id, timeline, "Study a language")
    for invalid in [uuid4(),
                    (await add_goal(db, execution.instance_id, timeline))[0],
                    (await add_goal(db, origin.instance_id, uuid4()))[0]]:
        with pytest.raises(HTTPException) as error:
            await work.request(origin.instance_id, req.model_copy(update={"goal_ref": invalid}))
        assert error.value.status_code == 409
    await db.execute("UPDATE aios.character_goal_attempt SET closed_at=now() WHERE attempt_id=$1", attempt)
    with pytest.raises(HTTPException):
        await work.request(origin.instance_id, req.model_copy(update={"goal_ref": goal}))
    await db.execute("UPDATE aios.character_goal_attempt SET closed_at=NULL WHERE attempt_id=$1", attempt)
    req = req.model_copy(update={"goal_ref": goal})
    result = await work.request(origin.instance_id, req)
    assert result["goal_ref"] == str(goal) and result["goal_association"] == "linked"
    assignment = await db.fetchval("SELECT assignment FROM aios.workspace_run WHERE run_id=$1", UUID(result["run_id"]))
    assignment = json.loads(assignment) if isinstance(assignment, str) else assignment
    assert assignment["goal_id"] == str(goal) and assignment["attempt_id"] == str(attempt)
    assert (await work.request(origin.instance_id, req))["run_id"] == result["run_id"]
    unlinked = await work.request(origin.instance_id, req.model_copy(update={
        "goal_ref": None, "request_id": uuid4(), "objective": "Inspect unrelated diagnostics"}))
    assert unlinked["goal_ref"] is None and unlinked["goal_association"].startswith("unlinked")


async def completed(db):
    values = await setup(db)
    work, origin, execution, bid, lid, source, req, profile = values
    receipt = await work.request(origin.instance_id, req)
    run = UUID(receipt["run_id"])
    result = {"result_summary": {"outcome": "partial", "summary": "Docker installed; container absent",
              "findings": ["Workspace is writable", "Docker daemon running"],
              "limitations": ["Container creation failed"]}, "report": "abcdefghij" * 600,
              "operator_secret": "must not expose unrelated fields"}
    await db.execute("UPDATE aios.workspace_run SET status='finished',result=$2::jsonb WHERE run_id=$1", run, json.dumps(result))
    return values, run, result


@pytest.mark.asyncio
async def test_result_pagination_revision_and_no_execution(db):
    values, run, result = await completed(db)
    work, origin, execution, bid, lid, source, req, profile = values
    await db.execute("UPDATE aios.workspace_binding SET last_seen_at=NULL WHERE binding_id=$1", bid)
    service = WorkEvidence(db)
    first = await service.read(origin.instance_id, WorkResultQuery(run_id=run, section="report", limit=1000))
    second = await service.read(origin.instance_id, WorkResultQuery(request_id=req.request_id, section="report",
                                 cursor=first["next_cursor"], limit=1000, revision=first["revision"]))
    assert first["text"] + second["text"] == result["report"][:2000]
    assert first["next_cursor"] == 1000 and second["next_cursor"] == 2000
    assert "operator_secret" not in json.dumps(first)
    with pytest.raises(HTTPException) as error:
        await service.read(execution.instance_id, WorkResultQuery(run_id=run))
    assert error.value.status_code == 404
    changed = {**result, "report": "updated report"}
    await db.execute("UPDATE aios.workspace_run SET result=$2::jsonb WHERE run_id=$1", run, json.dumps(changed))
    with pytest.raises(HTTPException) as error:
        await service.read(origin.instance_id, WorkResultQuery(run_id=run, revision=first["revision"]))
    assert error.value.status_code == 409
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1


@pytest.mark.asyncio
async def test_results_require_visible_nonsuperseded_ancestry(db):
    values, run, _ = await completed(db)
    work, origin, execution, bid, lid, source, req, profile = values
    service = WorkEvidence(db)
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    head = uuid4()
    await db.execute("INSERT INTO aios.ingest_event VALUES(2,'Cortana',NULL)")
    await db.execute("INSERT INTO aios.dag_node(node_id,timeline_id,event_id) VALUES($1,$2,2)", head, timeline)
    await db.execute("UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1", origin.instance_id, head)
    with pytest.raises(HTTPException):
        await service.read(origin.instance_id, WorkResultQuery(run_id=run))
    assert (await work.overlay(origin.instance_id, timeline))["result_tool"] is None
    await db.execute("INSERT INTO aios.dag_edge VALUES($1,$2,$3)", timeline, head, source)
    assert (await service.read(origin.instance_id, WorkResultQuery(run_id=run)))["status"] == "retrieved"
    assert (await work.overlay(origin.instance_id, timeline))["result_tool"]
    await db.execute("UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=1")
    with pytest.raises(HTTPException):
        await service.read(origin.instance_id, WorkResultQuery(run_id=run))
    assert (await work.overlay(origin.instance_id, timeline))["result_tool"] is None


@pytest.mark.asyncio
async def test_hud_retains_report_guidance_after_execution_is_revoked(db):
    values, run, _ = await completed(db)
    work, origin, execution, bid, lid, source, req, profile = values
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    await db.execute("UPDATE aios.character_environment_link SET enabled=false WHERE link_id=$1", lid)
    await db.execute("UPDATE aios.workspace_binding SET last_seen_at=NULL WHERE binding_id=$1", bid)
    overlay = await work.overlay(origin.instance_id, timeline)
    assert not overlay["capabilities"]
    assert overlay["result_tool"]
    assert "RETAINED WORK EVIDENCE" in render_hud_text({"work": overlay})
    assert (await WorkEvidence(db).read(origin.instance_id, WorkResultQuery(run_id=run)))["status"] == "retrieved"


@pytest.mark.asyncio
async def test_markup_and_native_reads_deliver_without_new_runs(db):
    values, run, _ = await completed(db)
    work, origin, execution, bid, lid, source, req, profile = values
    block = '<aios_action>' + json.dumps({"op": "read_work_result", "run_id": str(run), "section": "findings"}) + '</aios_action>'
    assert extract_work_intent(block) is None
    await db.execute("UPDATE aios.dag_node SET message_text=$2 WHERE node_id=$1", source, block)
    receipt = await work.from_source(origin.instance_id, source)
    assert receipt["status"] == "work_result" and "Workspace is writable" in receipt["result"]["text"]
    assert await work.from_source(origin.instance_id, source) == receipt
    overlay = await work.overlay(origin.instance_id, (await work.source(origin.instance_id, source))["source_timeline_id"])
    hud = render_hud_text({"work": overlay})
    assert "RETRIEVED WORK RESULT" in hud and "read_work_result" in hud
    registry = ActionRegistry()
    register_work_action(db, registry)
    native = await registry.get("work.result").handler(origin.instance_id, {"run_id": str(run), "section": "findings"})
    assert native["text"] == receipt["result"]["text"]
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 1
    await db.execute("UPDATE aios.workspace_run SET result='{}'::jsonb WHERE run_id=$1", run)
    assert (await work.from_source(origin.instance_id, source))["status"] == "needs_clarification"
    assert (await work.overlay(origin.instance_id, (await work.source(origin.instance_id, source))["source_timeline_id"]))["retrieved_result"] is None
    await db.execute("UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=1")
    with pytest.raises(HTTPException):
        await work.from_source(origin.instance_id, source)


@pytest.mark.asyncio
async def test_invalid_goal_reference_returns_actionable_markup_receipt(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    block = '<aios_action>' + json.dumps({"op": "request_work", **req.intent().model_dump(),
                                          "goal_ref": str(uuid4())}) + '</aios_action>'
    await db.execute("UPDATE aios.dag_node SET message_text=$2 WHERE node_id=$1", source, block)
    result = await work.from_source(origin.instance_id, source)
    assert result["status"] == "needs_clarification" and "goal_ref" in result["reason"]
    assert await db.fetchval("SELECT count(*) FROM aios.workspace_run") == 0


def test_native_read_route_requires_existing_request_key(monkeypatch):
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    monkeypatch.setenv("AIOS_WORK_REQUEST_API_KEY", "request-key")
    monkeypatch.setenv("AIOS_WORKSPACE_API_KEY", "worker-key")
    monkeypatch.setenv("AIOS_WORKSPACE_REVIEW_KEY", "review-key")
    with TestClient(app) as client:
        assert client.post(f"/agent/instance/{uuid4()}/work/result", json={"run_id": str(uuid4())}).status_code == 401
        async def read(self, instance_id, query):
            return {"status": "retrieved", "run_id": str(query.run_id)}
        monkeypatch.setattr(WorkEvidence, "read", read)
        run = uuid4()
        response = client.post(f"/agent/instance/{uuid4()}/work/result", json={"run_id": str(run)},
                               headers={"Authorization": "Bearer request-key"})
        assert response.status_code == 200 and response.json()["run_id"] == str(run)


@pytest.mark.asyncio
async def test_stored_goal_revalidation_is_opt_in_and_preserves_evidence(db):
    from aios_app.epistemic.goal_revalidation import revalidate_source_goals
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    goal, _ = await add_goal(db, origin.instance_id, timeline, "Say it once")
    good, _ = await add_goal(db, origin.instance_id, timeline, "Build operations journal")
    await db.execute("ALTER TABLE aios.character_agent_goal ADD COLUMN source_node_id uuid")
    await db.execute("CREATE TABLE aios.message_cognitive_commit(commit_id uuid,instance_id uuid,node_id uuid)")
    await db.execute("CREATE TABLE aios.message_cognitive_unit(unit_id uuid,commit_id uuid,claim_kind text,meta jsonb)")
    rows = [(goal, "I'll say it once, to the machine.", "say it once, to the machine"),
            (good, "I want to build an operations journal.", "to build an operations journal")]
    for text, objective, _ in CORTANA_GOAL_FRAGMENTS:
        gid, _ = await add_goal(db, origin.instance_id, timeline, objective)
        rows.append((gid, text, objective))
    for gid, text, objective in rows:
        unit, commit = uuid4(), uuid4()
        await db.execute("INSERT INTO aios.message_cognitive_commit VALUES($1,$2,$3)", commit, origin.instance_id, source)
        await db.execute("INSERT INTO aios.message_cognitive_unit VALUES($1,$2,'GOAL',$3::jsonb)",
                         unit, commit, json.dumps({"character_owned": True, "source_text": text, "objective": objective}))
        await db.execute("UPDATE aios.character_agent_goal SET source_node_id=$2,meta=$3::jsonb WHERE goal_id=$1",
                         gid, source, json.dumps({"source": "message_cognition", "source_unit_id": str(unit)}))
    dry = await revalidate_source_goals(db, origin.instance_id)
    assert {finding["objective"]: finding["reason"] for finding in dry} == {
        "say it once, to the machine": "current_utterance_or_reaction",
        **{objective: reason for _, objective, reason in CORTANA_GOAL_FRAGMENTS},
    }
    assert all(not finding["applied"] for finding in dry)
    assert await db.fetchval("SELECT count(*) FROM aios.character_agent_goal WHERE status='active'") == len(rows)
    applied = await revalidate_source_goals(db, origin.instance_id, apply=True)
    assert len(applied) == len(dry) and all(finding["applied"] for finding in applied)
    assert await db.fetchval("SELECT count(*) FROM aios.character_agent_goal WHERE status='cancelled'") == len(dry)
    assert await db.fetchval("SELECT status FROM aios.character_agent_goal WHERE goal_id=$1", good) == "active"
    assert await db.fetchval("SELECT count(*) FROM aios.message_cognitive_unit") == len(rows)
    assert await revalidate_source_goals(db, origin.instance_id, apply=True) == []
