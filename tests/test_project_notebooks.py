"""Pass A notebook boundary tests; no database server required."""
from unittest.mock import AsyncMock, MagicMock
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aios_app.agent.notebooks import NoteAppend, NoteQuery, NotebookStore
from aios_app.agent.work_admission import extract_work_action


def test_source_action_parsing_and_validation():
    goal = uuid4()
    source = '<aios_action>{"op":"notebook.append","goal_ref":"%s","entry_type":"question","content":"Check the evidence."}</aios_action>' % goal
    action = extract_work_action(source)
    note = NoteAppend.from_action(action)
    assert note.goal_ref == goal and note.entry_type == "question"
    assert note.content == "Check the evidence."
    assert extract_work_action("Here is an example:\n```\n" + source + "\n```") is None
    with pytest.raises(ValidationError):
        NoteAppend.from_action({**action, "unknown": "field"})
    with pytest.raises(ValidationError):
        NoteAppend.from_action({**action, "content": "x" * 4001})


def test_search_limits_are_bounded():
    goal = uuid4()
    assert NoteQuery(goal_ref=goal).limit == 10
    with pytest.raises(ValidationError):
        NoteQuery(goal_ref=goal, limit=100)


@pytest.mark.asyncio
async def test_cannot_write_without_active_attempt():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"source_timeline_id": uuid4(),
                                         "status": "completed", "attempt_id": None})
    store = NotebookStore(db)
    with pytest.raises(HTTPException) as exc:
        await store._scope(uuid4(), uuid4())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_worker_request_must_be_running_and_goal_scoped():
    db = MagicMock()
    db.fetchval = AsyncMock(return_value=None)
    store = NotebookStore(db)
    with pytest.raises(HTTPException) as exc:
        await store._work_scope(uuid4(), uuid4(), uuid4(), uuid4(), uuid4())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_only_reads_active_instance_timeline_goal():
    instance_id, goal_id, timeline_id = uuid4(), uuid4(), uuid4()
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"source_timeline_id": timeline_id,
                                          "status": "active", "attempt_id": uuid4()})
    db.fetch = AsyncMock(return_value=[])
    result = await NotebookStore(db).list(instance_id, NoteQuery(goal_ref=goal_id, query="glacial"))
    assert result["notes"] == []
    assert db.fetch.call_args.args[1:4] == (instance_id, timeline_id, goal_id)
    assert "plainto_tsquery" in db.fetch.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("count,limit,max_chars,has_more", [
    (1, 8, 6000, False),
    (2, 2, 6000, False),
    (3, 2, 6000, True),
    (2, 8, 200, True),
])
async def test_notebook_cursor_only_when_an_older_entry_remains(count, limit, max_chars, has_more):
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"source_timeline_id": uuid4()})
    db.fetch = AsyncMock(return_value=[
        {"entry_id": uuid4(), "entry_type": "note", "content": "x" * 200,
         "author_kind": "character", "source_node_id": uuid4(), "work_request_id": None,
         "created_at": datetime.now(timezone.utc)}
        for _ in range(count)
    ])
    result = await NotebookStore(db).list(
        uuid4(), NoteQuery(goal_ref=uuid4(), limit=limit, max_chars=max_chars)
    )
    assert db.fetch.call_args.args[-1] == limit + 1
    assert len(result["notes"]) == min(count, limit, max_chars // 200)
    assert result["next_before_id"] == (result["notes"][-1]["note_ref"] if has_more else None)
