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
            "knowledge_ready": 0,
            "topology_ready": 0,
            "acquisition_topology_ready": 0,
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


def test_claim_message_waits_for_all_retrieval_stages():
    db = FakeDB([
        {"section_id": uuid4(), "claims_extracted_at": object()},
        {
            "total": 2,
            "contextualized": 2,
            "normalized": 2,
            "knowledge_ready": 1,
            "topology_ready": 2,
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


def test_claim_message_ready_when_context_knowledge_and_topology_match():
    db = FakeDB([
        {"section_id": uuid4(), "claims_extracted_at": object()},
        {
            "total": 2,
            "contextualized": 2,
            "normalized": 2,
            "knowledge_ready": 2,
            "topology_ready": 2,
            "acquisition_topology_ready": 2,
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
