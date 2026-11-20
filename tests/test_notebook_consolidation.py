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


@pytest.mark.asyncio
async def test_run_persists_version_without_modifying_raw_notes():
    notebook_id, instance_id, note_id = uuid4(), uuid4(), uuid4()
    db = MagicMock()
    db.atomic = transaction
    db.fetchrow = AsyncMock(return_value={"notebook_id": notebook_id})
    db.fetch = AsyncMock(side_effect=[
        [{"entry_id": note_id, "entry_type": "question",
          "author_kind": "character", "content": "Unverified observation"}],
        [{"entry_id": note_id}],
    ])
    db.fetchval = AsyncMock(side_effect=[notebook_id, None, 1])
    db.execute = AsyncMock()
    curated = {
        "summary": "Research still needs verification.",
        "findings": [{"text": "An observation was recorded.",
                      "note_refs": [str(note_id)]}],
        "open_questions": ["Can the source be verified?"], "next_steps": [],
    }
    worker = NotebookConsolidator(db)
    worker.latest = AsyncMock(return_value={"status": "unconsolidated"})
    inference = MagicMock()
    inference.infer = AsyncMock(return_value=MagicMock(response=MagicMock(raw=curated)))
    with patch("aios_app.agent.notebook_consolidation.InferenceBroker",
               return_value=inference):
        result = await worker.run(instance_id, notebook_id)
    assert result == {"status": "curated", "revision": 1, "remaining": 0}
    assert "INSERT INTO aios.character_notebook_summary" in db.execute.call_args.args[0]
    assert "UPDATE aios.character_notebook_entry" not in db.execute.call_args.args[0]


@pytest.mark.asyncio
async def test_concurrent_summary_revision_is_not_overwritten():
    from aios_app.pipeline.jobs import JobDeferred
    notebook_id, instance_id, note_id = uuid4(), uuid4(), uuid4()
    db = MagicMock()
    db.atomic = transaction
    db.fetchrow = AsyncMock(return_value={"notebook_id": notebook_id})
    db.fetch = AsyncMock(return_value=[
        {"entry_id": note_id, "entry_type": "note",
         "author_kind": "worker", "content": "Still investigating"}
    ])
    db.fetchval = AsyncMock(side_effect=[notebook_id, 8])
    db.execute = AsyncMock()
    worker = NotebookConsolidator(db)
    worker.latest = AsyncMock(return_value={"status": "unconsolidated"})
    inference = MagicMock()
    inference.infer = AsyncMock(return_value=MagicMock(response=MagicMock(raw={
        "summary": "Work in progress", "findings": [],
        "open_questions": [], "next_steps": [],
    })))
    with patch("aios_app.agent.notebook_consolidation.InferenceBroker",
               return_value=inference):
        with pytest.raises(JobDeferred):
            await worker.run(instance_id, notebook_id)
    db.execute.assert_not_awaited()
