import asyncio
from uuid import uuid4

from aios_app.hud.readiness import source_node_retrieval_ready


class FakeDB:
    def __init__(self, rows):
        self.rows = list(rows)

    async def fetchrow(self, *_args, **_kwargs):
        return self.rows.pop(0)


def test_zero_claim_message_is_ready_after_extraction():
    db = FakeDB([
        {"section_id": uuid4(), "claims_extracted_at": object()},
        {
            "total": 0,
            "contextualized": 0,
            "normalized": 0,
            "character_required": 0,
            "character_ready": 0,
        },
    ])
    ready = asyncio.run(
        source_node_retrieval_ready(
            db,
            instance_id=uuid4(),
            node_id=uuid4(),
        )
    )
    assert ready is True


def test_claim_message_waits_for_character_knowledge():
    db = FakeDB([
        {"section_id": uuid4(), "claims_extracted_at": object()},
        {
            "total": 2,
            "contextualized": 2,
            "normalized": 2,
            "character_required": 2,
            "character_ready": 1,
        },
    ])
    ready = asyncio.run(
        source_node_retrieval_ready(
            db,
            instance_id=uuid4(),
            node_id=uuid4(),
        )
    )
    assert ready is False


def test_claim_message_waits_for_topology_projection():
    db = FakeDB([
        {"section_id": uuid4(), "claims_extracted_at": object()},
        {
            "total": 2,
            "contextualized": 2,
            "normalized": 2,
            "character_required": 1,
            "character_ready": 1,
        },
        {
            "total": 2,
            "claim_topology_ready": 1,
            "acquisition_required": 1,
            "acquisition_topology_ready": 1,
        },
    ])
    ready = asyncio.run(
        source_node_retrieval_ready(
            db,
            instance_id=uuid4(),
            node_id=uuid4(),
        )
    )
    assert ready is False


def test_claim_message_is_ready_when_cognition_and_topology_are_current():
    db = FakeDB([
        {"section_id": uuid4(), "claims_extracted_at": object()},
        {
            "total": 2,
            "contextualized": 2,
            "normalized": 2,
            "character_required": 1,
            "character_ready": 1,
        },
        {
            "total": 2,
            "claim_topology_ready": 2,
            "acquisition_required": 1,
            "acquisition_topology_ready": 1,
        },
    ])
    ready = asyncio.run(
        source_node_retrieval_ready(
            db,
            instance_id=uuid4(),
            node_id=uuid4(),
        )
    )
    assert ready is True
