from __future__ import annotations

from uuid import UUID

import pytest

from aios_app.agent.actions import default_action_registry
from aios_app.agent.capabilities import ResearchRouter


def test_default_registry_exposes_new_capability_layer():
    registry = default_action_registry(object())

    research = registry.capabilities_for("research")
    assert research["knowledge.lookup"]["class"] == "lookup"
    assert research["world.lookup"]["class"] == "lookup"
    assert "/char" in research["knowledge.lookup"]["description"]
    assert "/world" in research["world.lookup"]["description"]
    assert research["state.inspect"]["class"] == "lookup"
    assert research["research.search"]["class"] == "research"
    assert research["source.read"]["class"] == "research"
    assert research["research.search"]["result_mode"] == "return_to_cognition"
    assert research["corpus.acquire"]["result_mode"] == "final"

    communication = registry.capabilities_for("communication")
    assert "state.inspect" in communication
    assert "research.search" not in communication
    assert "source.read" not in communication
    assert "world.lookup" not in communication


def test_action_registry_has_no_model_facing_schema_api():
    registry = default_action_registry(object())
    assert not hasattr(registry, "schemas_for")
    assert "knowledge.lookup" in registry.capabilities_for("research")


class _ReadDB:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.row


@pytest.mark.asyncio
async def test_source_read_requires_exposure_for_same_instance():
    instance_id = UUID("00000000-0000-0000-0000-000000000001")
    research_id = UUID("00000000-0000-0000-0000-000000000002")
    section_id = UUID("00000000-0000-0000-0000-000000000003")
    db = _ReadDB(None)

    with pytest.raises(PermissionError):
        await ResearchRouter(db).read_source(
            instance_id=instance_id,
            research_id=research_id,
            section_id=section_id,
        )

    assert db.calls
    _, args = db.calls[0]
    assert args == (research_id, section_id, instance_id)


@pytest.mark.asyncio
async def test_source_read_returns_reference_not_durable_knowledge():
    instance_id = UUID("00000000-0000-0000-0000-000000000001")
    research_id = UUID("00000000-0000-0000-0000-000000000002")
    section_id = UUID("00000000-0000-0000-0000-000000000003")
    document_id = UUID("00000000-0000-0000-0000-000000000004")
    db = _ReadDB({
        "section_id": section_id,
        "document_id": document_id,
        "title": "Reference",
        "heading": "Details",
        "content": "Full source text",
        "score": 0.8,
        "rank": 1,
        "scopes": ["fiction.test.reference"],
    })

    result = await ResearchRouter(db).read_source(
        instance_id=instance_id,
        research_id=research_id,
        section_id=section_id,
    )

    assert result["text"] == "Full source text"
    assert result["durable_knowledge"] is False
    assert result["research_id"] == str(research_id)
    assert result["section_id"] == str(section_id)


def test_action_spec_defaults_to_local_execution():
    import inspect
    from aios_app.agent.actions import ActionSpec
    assert inspect.signature(ActionSpec).parameters["execution_mode"].default == "local"


def test_worker_capability_metadata_exposes_execution_mode():
    from aios_app.agent.actions import ActionRegistry, ActionSpec

    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="compute.exec",
        schema={"type": "object"},
        side_effect_class="external_sensitive",
        allowed_worker_classes=frozenset({"executive"}),
        handler=None,
        execution_mode="worker",
        result_mode="asynchronous",
        capability_class="compute",
    ))
    capability = registry.capabilities_for("executive")["compute.exec"]
    assert capability["execution_mode"] == "worker"
    assert capability["result_mode"] == "asynchronous"


def test_character_and_world_lookup_are_distinct_handlers():
    registry = default_action_registry(object())
    character = registry.get("knowledge.lookup")
    world = registry.get("world.lookup")

    assert character is not None
    assert world is not None
    assert character.handler is not world.handler
    assert character.side_effect_class == "read_only"
    assert world.side_effect_class == "read_only"
    assert character.result_mode == "return_to_cognition"
    assert world.result_mode == "return_to_cognition"


def test_world_lookup_exposes_domain_without_granting_access():
    registry = default_action_registry(object())
    spec = registry.get("world.lookup")

    assert spec is not None
    assert "domain" in spec.schema["properties"]
    assert spec.capability_class == "lookup"
    assert "ownership" in spec.description


def test_memory_inspect_is_bounded_and_research_only():
    registry = default_action_registry(object())
    inspect_spec = registry.get("memory.inspect")
    hint_spec = registry.get("memory.relation_hint")

    assert inspect_spec is not None
    assert inspect_spec.schema["properties"]["proposition_ids"]["maxItems"] == 2
    assert inspect_spec.side_effect_class == "read_only"
    assert "research" in inspect_spec.allowed_worker_classes
    assert "communication" not in inspect_spec.allowed_worker_classes

    assert hint_spec is not None
    assert hint_spec.side_effect_class == "internal_write"
    judgments = hint_spec.schema["properties"]["judgment"]["enum"]
    assert "none" in judgments
    assert "uncertain" in judgments
    assert "garbage_both" in judgments
