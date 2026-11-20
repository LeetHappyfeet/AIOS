"""Pass C: scope-checked hybrid project notebook retrieval."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from aios_app.agent.notebook_recall import ProjectNoteRecall
from aios_app.agent.notebook_vectors import semantic_candidates


@pytest.mark.asyncio
async def test_no_project_notebook_does_not_query_vector_store():
    instance_id, goal_id, timeline_id = uuid4(), uuid4(), uuid4()
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=None)
    with patch("aios_app.agent.notebook_recall.semantic_candidates",
               new_callable=AsyncMock) as vector:
        result = await ProjectNoteRecall(db).retrieve(
            instance_id, goal_id, timeline_id, "geology")
    assert result["status"] == "no_project_notebook"
    vector.assert_not_awaited()


@pytest.mark.asyncio
async def test_lexical_results_do_not_require_vector_lookup():
    instance_id, goal_id, timeline_id, notebook_id, entry_id = (uuid4() for _ in range(5))
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"notebook_id": notebook_id})
    db.fetch = AsyncMock(return_value=[
        {"entry_id": entry_id, "content": "Glacial deposit mentioned",
         "entry_type": "observation", "author_kind": "character"}
    ])
    with patch("aios_app.agent.notebook_recall.semantic_candidates",
               new_callable=AsyncMock) as vector, patch(
                   "aios_app.agent.notebook_consolidation.NotebookConsolidator.latest",
                   new_callable=AsyncMock, return_value={"status": "unconsolidated"}):
        result = await ProjectNoteRecall(db).retrieve(
            instance_id, goal_id, timeline_id, "glacial", limit=1)
    assert len(result["notes"]) == 1
    assert result["notes"][0]["note_ref"] == str(entry_id)
    vector.assert_not_awaited()
    assert db.fetch.call_args.args[1:5] == (
        notebook_id, instance_id, timeline_id, goal_id)


@pytest.mark.asyncio
async def test_qdrant_hits_are_rehydrated_only_from_authorized_sql_scope():
    notebook_id, instance_id, timeline_id, goal_id = (uuid4() for _ in range(4))
    allowed, foreign = uuid4(), uuid4()
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[
        {"entry_id": allowed, "content": "Known notebook note",
         "entry_type": "note", "author_kind": "worker"}
    ])
    with patch("aios_app.agent.notebook_vectors._search_blocking",
               return_value=[
                   (str(foreign), 0.99, {}),
                   (str(allowed), 0.90, {}),
               ]):
        result = await semantic_candidates(
            db, instance_id=instance_id, timeline_id=timeline_id,
            goal_id=goal_id, notebook_id=notebook_id,
            query="notes", limit=3)
    assert [r["entry_id"] for r in result] == [allowed]
    sql = db.fetch.call_args.args[0]
    assert "notebook_id=$1" in sql
    assert "instance_id=$2" in sql
    assert "source_timeline_id=$3" in sql
    assert "goal_id=$4" in sql


@pytest.mark.asyncio
async def test_hybrid_recall_deduplicates_and_bounds_excerpts():
    instance_id, goal_id, timeline_id, notebook_id, entry_a, entry_b = (uuid4() for _ in range(6))
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"notebook_id": notebook_id})
    db.fetchval = AsyncMock(return_value=1)
    db.fetch = AsyncMock(return_value=[
        {"entry_id": entry_a, "content": "a" * 2000,
         "entry_type": "note", "author_kind": "character"}
    ])
    with patch("aios_app.agent.notebook_recall.semantic_candidates",
               new_callable=AsyncMock, return_value=[
                   {"entry_id": entry_a, "content": "a" * 2000,
                    "entry_type": "note", "author_kind": "character"},
                   {"entry_id": entry_b, "content": "b" * 2000,
                    "entry_type": "note", "author_kind": "worker"},
               ]), patch("aios_app.agent.notebook_consolidation.NotebookConsolidator.latest",
                         new_callable=AsyncMock, return_value={"status": "unconsolidated"}):
        result = await ProjectNoteRecall(db).retrieve(
            instance_id, goal_id, timeline_id, "evidence",
            limit=2, max_chars=200)
    assert [n["note_ref"] for n in result["notes"]] == [str(entry_a)]
    assert len(result["notes"][0]["excerpt"]) == 200
    assert result["notes"][0]["truncated"]
