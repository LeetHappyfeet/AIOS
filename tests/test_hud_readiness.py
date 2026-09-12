import asyncio
from uuid import uuid4

from aios_app.hud.readiness import (
    source_node_retrieval_ready,
    source_node_topology_ready,
)


class FakeDB:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append((query, args, kwargs))
        return self.row


class SequenceDB:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append((query, args, kwargs))
        return self.rows.pop(0)

    async def execute(self, query, *args, **kwargs):
        self.calls.append((query, args, kwargs))
        return "UPDATE 1"


def test_message_is_not_ready_without_cognitive_commit():
    db = FakeDB(None)
    ready = asyncio.run(
        source_node_retrieval_ready(
            db,
            instance_id=uuid4(),
            node_id=uuid4(),
        )
    )
    assert ready is False
    assert "message_cognitive_commit" in db.calls[0][0]


def test_message_is_ready_with_cognitive_commit_even_before_enrichment():
    db = FakeDB({"?column?": 1})
    ready = asyncio.run(
        source_node_retrieval_ready(
            db,
            instance_id=uuid4(),
            node_id=uuid4(),
        )
    )
    assert ready is True
    assert len(db.calls) == 1


def test_no_source_node_is_trivially_ready():
    db = FakeDB(None)
    ready = asyncio.run(
        source_node_retrieval_ready(
            db,
            instance_id=uuid4(),
            node_id=None,
        )
    )
    assert ready is True
    assert db.calls == []


def test_enrichment_is_not_ready_before_document_section_exists():
    db = FakeDB(None)
    ready = asyncio.run(
        source_node_topology_ready(
            db,
            instance_id=uuid4(),
            node_id=uuid4(),
        )
    )
    assert ready is False
    assert len(db.calls) == 1
    assert "document_section" in db.calls[0][0]


def test_enrichment_is_not_ready_before_claim_extraction_finishes():
    db = FakeDB({"claims_extracted_at": None})
    ready = asyncio.run(
        source_node_topology_ready(
            db,
            instance_id=uuid4(),
            node_id=uuid4(),
        )
    )
    assert ready is False
    assert len(db.calls) == 1


def test_zero_claim_message_is_enrichment_ready_after_extraction():
    node_id = uuid4()
    db = SequenceDB(
        [
            {"claims_extracted_at": object()},
            {
                "total": 0,
                "claim_topology_ready": 0,
                "acquisition_required": 0,
                "acquisition_topology_ready": 0,
            },
            {"event_id": 17},
        ]
    )
    ready = asyncio.run(
        source_node_topology_ready(
            db,
            instance_id=uuid4(),
            node_id=node_id,
        )
    )
    assert ready is True
    assert any("enrichment_ready_node_id" in call[0] for call in db.calls)
