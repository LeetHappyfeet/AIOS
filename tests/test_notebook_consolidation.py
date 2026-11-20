"""Pass B tests: versioned, scoped, non-chat consolidation."""
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from aios_app.agent.notebook_consolidation import (
    NotebookConsolidator, validate_curated, AUTOMATIC_THRESHOLD,
)


def test_curated_contract_rejects_fabricated_references():
    note = str(uuid4())
    valid = {
        "summary": "Unverified work in progress.",
        "findings": [{"text": "A file was retrieved.", "note_refs": [note]}],
        "open_questions": ["What does the file establish?"],
        "next_steps": ["Inspect the file."],
    }
    assert validate_curated(valid, {note}) == valid
    with pytest.raises(ValueError):
        validate_curated(valid, {str(uuid4())})
    with pytest.raises(ValueError):
        validate_curated({**valid, "extra": True}, {note})
    with pytest.raises(ValueError):
        validate_curated({**valid, "findings": [{"text": "Unsupported", "note_refs": []}]}, {note})
    with pytest.raises(ValueError):
        validate_curated({**valid, "open_questions": ["x" * 241]}, {note})


@asynccontextmanager
async def transaction():
    yield


@pytest.mark.asyncio
async def test_threshold_does_not_infer_or_queue_on_normal_write():
    db = MagicMock()
    db.atomic = transaction
    db.fetchrow = AsyncMock(return_value={"notebook_id": uuid4()})
    db.fetchval = AsyncMock(side_effect=[AUTOMATIC_THRESHOLD - 1, None])
    instance_id, notebook_id = uuid4(), uuid4()
    worker = NotebookConsolidator(db)
    with patch("aios_app.agent.notebook_consolidation.enqueue_job", new_callable=AsyncMock) as queue:
        result = await worker.queue(instance_id, notebook_id)
        assert result["status"] == "unchanged"
        queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_request_allows_single_entry():
    db = MagicMock()
    db.atomic = transaction
    db.fetchrow = AsyncMock(return_value={"notebook_id": uuid4()})
    db.fetchval = AsyncMock(side_effect=[1, None])
    with patch("aios_app.agent.notebook_consolidation.enqueue_job", new_callable=AsyncMock,
               return_value=uuid4()) as queue:
        result = await NotebookConsolidator(db).queue(uuid4(), uuid4(), explicit=True)
        assert result["status"] == "queued"
        queue.assert_awaited_once()


@pytest.mark.asyncio
async def test_latest_summary_is_private_to_instance():
    notebook_id, instance_id = uuid4(), uuid4()
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=None)
    result = await NotebookConsolidator(db).latest(instance_id, notebook_id)
    assert result == {"status": "unconsolidated"}
    assert db.fetchrow.call_args.args[1:] == (notebook_id, instance_id)
