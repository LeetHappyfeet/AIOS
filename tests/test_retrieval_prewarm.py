from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from aios_app.epistemic.cognitive_context import (
    CognitiveAttentionInputs,
    CognitiveContextService,
)


class _FakeRetriever:
    def __init__(self):
        self.calls: list[str] = []

    async def retrieve_character_knowledge(
        self,
        context,
        scorer,
        *,
        mode,
        focus_text="",
        goals=(),
        max_hops=None,
        limit=None,
    ):
        self.calls.append(mode)
        return [
            {
                "proposition_id": f"{mode}-1",
                "topic_key": "topic",
                "text": f"prepared {mode}",
                "subject_norm": "Alex",
                "predicate_norm": "remembers",
                "object_norm": mode,
                "claim_kind": "MEMORY" if mode == "memory" else "BELIEF",
                "confidence": 0.9,
                "effective_confidence": 0.9,
                "conflicts": [],
            }
        ]


class _Score:
    total = 1.0

    def as_dict(self):
        return {"total": self.total}


class _Scorer:
    def score(self, item, **kwargs):
        return _Score()


def _context(node_id):
    instance_id = uuid4()
    return SimpleNamespace(
        instance_id=instance_id,
        character_id="Alex",
        source_head_node_id=node_id,
        state_version=3,
        lineage_instance_ids=(instance_id,),
        world_id=uuid4(),
    )


def _attention():
    return CognitiveAttentionInputs(
        recent_newest=[],
        visible_source_node_ids=frozenset(),
        focus_text="Where did we hide the key?",
        plugin_focus_text="",
        retrieval_focus_text="Where did we hide the key?",
        goals=["find the key"],
    )


def _profile():
    return SimpleNamespace(
        entity_hops=2,
        semantic_retrieval_limit=60,
        deep_memory_limit=20,
    )


@pytest.mark.asyncio
async def test_prepare_retrieval_reuses_exact_source_head_cache():
    service = CognitiveContextService(object())
    fake = _FakeRetriever()
    service.retriever = fake
    context = _context(uuid4())

    first = await service.prepare_retrieval(
        context,
        _Scorer(),
        _attention(),
        _profile(),
    )
    second = await service.prepare_retrieval(
        context,
        _Scorer(),
        _attention(),
        _profile(),
    )

    assert first is second
    assert fake.calls == ["memory", "belief", "goal", "event", "rule"]


@pytest.mark.asyncio
async def test_prepare_retrieval_does_not_cross_source_heads():
    service = CognitiveContextService(object())
    fake = _FakeRetriever()
    service.retriever = fake
    first_context = _context(uuid4())
    second_context = SimpleNamespace(**vars(first_context))
    second_context.source_head_node_id = uuid4()

    await service.prepare_retrieval(
        first_context,
        _Scorer(),
        _attention(),
        _profile(),
    )
    await service.prepare_retrieval(
        second_context,
        _Scorer(),
        _attention(),
        _profile(),
    )

    assert fake.calls == [
        "memory", "belief", "goal", "event", "rule",
        "memory", "belief", "goal", "event", "rule",
    ]
