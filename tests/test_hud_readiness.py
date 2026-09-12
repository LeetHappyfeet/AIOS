import asyncio
from uuid import uuid4

from aios_app.hud.readiness import source_node_retrieval_ready


class FakeDB:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append((query, args, kwargs))
        return self.row


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
