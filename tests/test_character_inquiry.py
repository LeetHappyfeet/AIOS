"""Read-only inquiry contracts, source isolation, bounded planner and triggers."""
from types import SimpleNamespace
from uuid import UUID
import pytest

from aios_app.epistemic.inquiry.contracts import InquiryDemand, InquiryEvidence, InquiryHit
from aios_app.epistemic.inquiry.trigger import demand_from_v11_rejection, should_escalate
from aios_app.epistemic.inquiry.planner import (
    build_query_hud, validate_query_payload)
from aios_app.epistemic.inquiry.lookup import InquiryLookup

INSTANCE = UUID("00000000-0000-0000-0000-000000000001")
NODE = UUID("00000000-0000-0000-0000-000000000002")
TIMELINE = UUID("00000000-0000-0000-0000-000000000003")


def demand(**overrides):
    values = dict(instance_id=INSTANCE, origin="character_cognition",
                  uncertainty_kind="explicit_question", question="What does this mean?",
                  evidence_revision="rev1", source_node_id=NODE)
    values.update(overrides)
    return InquiryDemand(**values)


def test_fingerprint_revision_and_instance_isolation():
    first = demand()
    assert first.fingerprint == demand().fingerprint
    assert first.fingerprint != demand(evidence_revision="rev2").fingerprint
    assert first.fingerprint != demand(instance_id=NODE).fingerprint
    assert InquiryDemand.from_dict(first.as_dict()) == first


def test_source_local_scope_requires_anchor():
    with pytest.raises(ValueError, match="anchored"):
        demand(source_node_id=None, evidence_scope="source_local")


def test_only_eligible_v11_diagnostic_becomes_source_inquiry():
    kwargs = dict(instance_id=INSTANCE, source_node_id=NODE,
                  source_text="I'm going to start collecting them.",
                  source_index=0, admission_version="v11")
    good = demand_from_v11_rejection(
        **kwargs,
        rejection_reason="source_admission:unresolved_reference:objective_contains_unresolved_reference")
    assert good is not None
    assert good.evidence_scope == "source_local"
    assert good.uncertainty_kind == "unresolved_reference"
    for reason in ("source_admission:scene_only:transient_scene_action",
                   "source_admission:nonliteral_statement:narrator_implied_or_imagined_first_person",
                   "source_admission:insufficient_commitment:empty_objective"):
        assert demand_from_v11_rejection(**kwargs, rejection_reason=reason) is None


def test_source_local_never_escalates_automatically():
    src = demand(evidence_scope="source_local", uncertainty_kind="unresolved_reference")
    assert not should_escalate(src, "resolved")
    # The worker decides whether to request optional planner assistance.
    assert should_escalate(src, "partial")
    assert not should_escalate(src, "partial", existing_model_calls=1)


def test_planner_cannot_cross_source_admission_boundary():
    src = demand(evidence_scope="source_local", uncertainty_kind="unresolved_reference")
    with pytest.raises(ValueError, match="forbidden"):
        validate_query_payload({"question": "Who?", "query": "Renamon bags",
                                "target": "personal"}, src)
    assert validate_query_payload({"question": "What does them refer to?",
            "query": "collecting them", "target": "source"}, src)[2] == "source"
    for query in ("https://example.com", "\u0000unsafe", "a" * 141):
        with pytest.raises(ValueError):
            validate_query_payload({"question": "What?", "query": query,
                                    "target": "auto"}, demand())


def test_query_hud_is_micro_and_read_only():
    evidence = InquiryEvidence("partial", (
        InquiryHit("source_dag", str(NODE), "A" * 2000),))
    prompt = build_query_hud(demand(), evidence, character_id="Renamon")
    assert len(prompt) <= 1800
    assert "Do not answer" in prompt
    assert "CHARACTER: Renamon" in prompt
    assert "No actions" in prompt


@pytest.mark.asyncio
async def test_source_lookup_refuses_non_source_backend(monkeypatch):
    from aios_app.epistemic.inquiry import lookup as module
    async def context(self, instance_id):
        return SimpleNamespace(timeline_id=TIMELINE,source_timeline_id=TIMELINE)
    monkeypatch.setattr(module.HUDContextResolver, "resolve", context)
    class DB:
        async def fetch(self, *a):
            raise AssertionError("forbidden backend touched")
    with pytest.raises(PermissionError):
        await InquiryLookup(DB()).search(
            demand(evidence_scope="source_local", uncertainty_kind="unresolved_reference"),
            target="personal")


@pytest.mark.asyncio
async def test_source_ancestry_keeps_timeline_and_no_admission(monkeypatch):
    from aios_app.epistemic.inquiry import lookup as module
    async def context(self, instance_id):
        return SimpleNamespace(timeline_id=TIMELINE,source_timeline_id=TIMELINE)
    monkeypatch.setattr(module.HUDContextResolver, "resolve", context)
    class DB:
        async def fetch(self, sql, source_node, timeline):
            assert source_node == NODE and timeline == TIMELINE
            assert "parent_node_id" in sql and "a.depth < 3" in sql
            return [
                dict(node_id=NODE,timeline_id=TIMELINE,speaker_id="Renamon",
                     speaker_role="character",message_text="I'll collect them.",depth=0),
                dict(node_id=INSTANCE,timeline_id=TIMELINE,speaker_id="User",
                     speaker_role="user",message_text="Those old postcards.",depth=1)]
    result = await InquiryLookup(DB()).search(
        demand(evidence_scope="source_local", uncertainty_kind="unresolved_reference"))
    assert result.status == "partial"
    assert len(result.hits) == 2
    assert not any(hit.durable_knowledge for hit in result.hits)
    assert result.hits[1].provenance["depth"] == 1
