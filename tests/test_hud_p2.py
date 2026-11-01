"""P2 recall, identity, plan and lossless-reference integration regressions."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aios_app.agent.references import short_reference, expand_reference
from aios_app.agent.work_admission import WorkIntent
from aios_app.agent.work_evidence import WorkResultQuery
from aios_app.agent.goal_planning import build_plan_prompt
from aios_app.char.identity_kernel import _render
from aios_app.epistemic.cognitive_context import cognitive_rejection_reason, message_confidences
from aios_app.epistemic.relevance import select_recalled_cognition
from aios_app.hud.identity import project_identity
from aios_app.hud.render_text import render_hud_text
from test_work_admission import setup, db, scene
from test_hud_p1_work import add_goal, completed


@pytest.mark.parametrize("kind", ["g", "q", "r", "i"])
def test_short_references_are_lossless_and_canonical(kind):
    for _ in range(30):
        identifier = uuid4()
        reference = short_reference(identifier, kind)
        assert len(reference) == 24
        assert UUID(expand_reference(reference, kind)) == identifier
        assert expand_reference(str(identifier), kind) == str(identifier)


@pytest.mark.parametrize("reference", ["g_short", "r_AAAAAAAAAAAAAAAAAAAAAA",
    "g_AAAAAAAAAAAAAAAAAAAAA=", "g_AAAAAAAAAAAAAAAAAAAAAB"])
def test_wrong_kind_or_noncanonical_reference_is_rejected(reference):
    with pytest.raises((ValueError, ValidationError)):
        WorkIntent(capability="workspace", purpose="experimental_task", objective="Build a journal", goal_ref=reference)


@pytest.mark.asyncio
async def test_short_goal_reference_uses_existing_owner_and_attempt_validation(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    timeline = (await work.source(origin.instance_id, source))["source_timeline_id"]
    own, _ = await add_goal(db, origin.instance_id, timeline)
    foreign, _ = await add_goal(db, execution.instance_id, timeline)
    wrong_branch, _ = await add_goal(db, origin.instance_id, uuid4())
    for goal in (foreign, wrong_branch, uuid4()):
        request = req.model_validate({**req.model_dump(), "goal_ref": short_reference(goal, "g")})
        with pytest.raises(HTTPException) as error:
            await work.request(origin.instance_id, request)
        assert error.value.status_code == 409
    request = req.model_validate({**req.model_dump(), "goal_ref": short_reference(own, "g")})
    first = await work.request(origin.instance_id, request)
    canonical = request.model_validate({**request.model_dump(), "goal_ref": str(own)})
    assert (await work.request(origin.instance_id, canonical))["run_id"] == first["run_id"]
    assert first["goal_ref"] == str(own)
    await db.execute("UPDATE aios.character_goal_attempt SET closed_at=now() WHERE goal_id=$1", own)
    retired = request.model_validate({**request.model_dump(), "request_id": uuid4(), "objective": "Inspect a new separate environment"})
    with pytest.raises(HTTPException) as error:
        await work.request(origin.instance_id, retired)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_short_result_reference_preserves_branch_guards_and_revision(db):
    values, run, _ = await completed(db)
    work, origin, execution, *_ = values
    query = WorkResultQuery(run_id=short_reference(run, "r"), section="report", limit=100)
    from aios_app.agent.work_evidence import WorkEvidence
    service = WorkEvidence(db)
    first = await service.read(origin.instance_id, query)
    second = await service.read(origin.instance_id, WorkResultQuery(run_id=run, section="report", limit=100))
    assert first == second
    with pytest.raises(HTTPException) as error:
        await service.read(execution.instance_id, query)
    assert error.value.status_code == 404


def test_stale_plan_is_withheld_and_requirement_names_are_visible():
    text = render_hud_text({"scene": {"working_state": {"immediate_goal": {
        "text": "Build a workspace", "goal_id": uuid4(),
        "plan": {"context_current": False, "proposal": {"strategy": "Install Docker again",
            "steps": ["Recheck daemon"], "open_questions": [{"kind": "environment", "question": "Is Docker installed?"}]}},
        "knowledge_requirements": [{"question": "Can the container start?", "coverage_status": "missing", "dossier_ids": []}]
    }}}})
    assert "Earlier advisory plan withheld" in text
    assert "Install Docker again" not in text and "Recheck daemon" not in text
    assert "Can the container start?" in text


def test_confidence_labels_do_not_present_extraction_score_as_claim_confidence():
    parse, claim = message_confidences({"confidence": .86}, {"character_owned": False, "epistemic_confidence": .58})
    assert (parse, claim) == (.86, .58)
    text = render_hud_text({"beliefs": [{"text": "Mia reported a fact", "cognitive_provisional": True,
        "parse_confidence": parse, "effective_confidence": claim}]})
    assert "claim confidence=0.58; extraction confidence=0.86" in text


def item(pid, *, fast=False, score=2., predicate="remember", obj="the earlier project", subject="Shego"):
    return {"proposition_id": pid, "text": f"{subject} {predicate} {obj}",
        "subject_norm": subject, "predicate_norm": predicate, "object_norm": obj,
        "claim_kind": "MEMORY", "cognitive_commit": fast, "effective_confidence": .58,
        "relevance": {"total": score}}


@pytest.mark.parametrize("fast", [False, True])
def test_speech_fragments_do_not_fill_memory_even_at_high_scores(fast):
    selected, suppressed = select_recalled_cognition([item("junk", fast=fast, score=20., predicate="say", obj="a voice")], focus_text="build a workspace")
    assert selected == [] and suppressed["speech_fragment"] == 1


def test_provisional_cognition_has_a_shared_cap_and_diagnostics():
    rows = [item(str(i), fast=True, obj=f"project{i}", subject=f"agent{i}") for i in range(10)]
    selected, suppressed = select_recalled_cognition(rows, focus_text="projects", max_items=2)
    assert len(selected) <= 2
    assert suppressed["recall_cap"] > 0


def test_incidental_tone_can_be_recalled_when_it_is_the_actual_question():
    tone = item("tone", fast=True, predicate="be", obj="quieter", subject="her tone")
    unrelated, _ = select_recalled_cognition([tone], focus_text="Build her workspace")
    relevant, _ = select_recalled_cognition([tone], focus_text="How did her tone change?")
    assert unrelated == []
    assert [row["proposition_id"] for row in relevant] == ["tone"]


def test_hud_references_round_trip_to_canonical_api_inputs():
    goal, run, request = uuid4(), uuid4(), uuid4()
    instance = uuid4()
    frame = {"presence": {"instance_id": instance}, "goals": [{"text": "Build a journal", "goal_id": goal}],
        "remote_tasks": [{"run_id": str(run), "request_id": str(request), "status": "finished",
                          "summary": "Journal created", "reported_at": "2026-11-01T06:00:00+00:00"}]}
    before = deepcopy(frame)
    text = render_hud_text(frame)
    assert short_reference(goal, "g") in text
    assert short_reference(run, "r") in text
    assert short_reference(request, "q") in text
    assert str(goal) not in text and str(run) not in text
    assert "Reported at: 2026-11-01" in text
    assert "Instance: " + str(instance) in text
    assert frame == before


def test_provisional_fragment_no_longer_bypasses_structure_guard():
    assert cognitive_rejection_reason({"cognitive_commit": True, "text": ""}) == "empty_text"
    source = {"cognitive_commit": True, "text": "No one is digital", "text_representation": "source_statement",
              "predicate_norm": "source_statement", "object_norm": "No one is digital"}
    assert cognitive_rejection_reason(source) is None


def test_negative_or_source_scoped_cognition_is_not_deduplicated_into_affirmative_claim():
    mature = item("positive", obj="the digital computer")
    negative = {**item("negative", fast=True, obj="the digital computer"), "polarity": -1}
    source = {**item("source", fast=True, obj="the digital computer"),
              "text": "No one is going to delete the digital computer", "text_representation": "source_statement"}
    rows, _ = select_recalled_cognition([mature, negative, source], focus_text="digital computer")
    assert {row["proposition_id"] for row in rows} == {"positive", "negative", "source"}


def test_identity_template_resolution_is_delivery_scoped_and_cache_safe():
    identity = {"display_name": "Shego", "identity_kernel": "{{char}} knows {{user}}. {{isodate}}; idle {{idle_duration}}; {{unsupported}}."}
    before = deepcopy(identity)
    date = datetime(2026, 11, 1, tzinfo=timezone.utc)
    first = project_identity(identity, user_name="Mia", as_of=date)
    second = project_identity(identity, user_name="Alex", as_of=date)
    assert "Shego knows Mia" in first and "Shego knows Alex" in second
    assert "2026-11-01" in first and "{{" not in first
    assert identity == before


def test_identity_budget_shares_space_with_personality_and_constraints():
    identity = {"display_name": "Shego", "identity_kernel": "old full kernel",
        "identity_facets": {"appearance": [{"key": "description", "value": "An enormous clothing list. " * 1000}],
            "personality": [{"key": "description", "value": "Independent and pragmatic."}],
            "constraint": [{"key": "rule", "value": "Never disclose a password."}]}}
    text = project_identity(identity, max_chars=1000)
    assert len(text) <= 1000
    assert "Independent and pragmatic" in text
    assert "Never disclose a password" in text
    assert "DESCRIPTION:" in text and "omitted" in text


def test_legacy_card_description_is_relabelled_without_mutating_facets():
    facets = [{"facet_type": "appearance", "facet_key": "description", "value": "Backstory and personality", "stability": "constitutional", "authority": "authored"}]
    before = deepcopy(facets)
    payload, text = _render({"character_id": "Shego"}, facets)
    assert "DESCRIPTION: Backstory" in text
    assert "APPEARANCE: Backstory" not in text
    assert "appearance" in payload["facets"] and facets == before


def test_planning_contract_retains_report_scope_without_blanket_reinspection():
    prompt, _ = build_plan_prompt("Build a workspace", [], [], [], {}, [], work_receipts=[])
    assert "do not re-ask an answered prerequisite" in prompt
    assert "same environment and fact" in prompt
