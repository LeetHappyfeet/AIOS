import pytest

from aios_app.pipeline import jobs


class RecordingDb:
    def __init__(self):
        self.sql = ""
        self.args = ()

    async def execute_returning_row(self, sql, *args):
        self.sql = sql
        self.args = args
        return None


@pytest.mark.asyncio
async def test_rdf_claims_rank_background_after_foreground() -> None:
    db = RecordingDb()

    result = await jobs.fetch_next_job(
        db,
        worker_id="test-worker",
        resource_class="RDF",
        lease_seconds=120,
    )

    assert result is None
    background_rank = db.sql.index("q.scheduling_lane = 'BACKGROUND'")
    priority_rank = db.sql.index("q.priority ASC")
    assert background_rank < priority_rank
    assert db.args[0] == "RDF"


@pytest.mark.asyncio
async def test_rebalance_demotes_scope_projection_to_background_priority() -> None:
    db = RecordingDb()

    await jobs.rebalance_queued_priorities(db)

    assert "WHEN 'project_semantic_scope' THEN 200" in db.sql
