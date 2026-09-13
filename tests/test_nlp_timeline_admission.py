from __future__ import annotations

import asyncio
from uuid import uuid4

from aios_app.pipeline.jobs import _partition_key_for_enqueue, fetch_next_job


class TimelineDb:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.claim_sql = ""

    async def fetchrow(self, sql, *args):
        if "SELECT dn.timeline_id" in sql:
            return {"timeline_id": uuid4()}
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 0"

    async def execute_returning_row(self, sql, *args):
        self.claim_sql = sql
        return None


def test_decompose_enqueue_uses_timeline_partition():
    db = TimelineDb()
    partition = asyncio.run(
        _partition_key_for_enqueue(
            db,
            job_type="decompose_claim_frames",
            payload={"claim_id": str(uuid4())},
        )
    )
    assert partition is not None
    assert partition.startswith("timeline:")


def test_nlp_claim_backfills_legacy_jobs_and_excludes_running_same_timeline():
    db = TimelineDb()
    asyncio.run(
        fetch_next_job(
            db,
            worker_id="test:NLP:0",
            resource_class="NLP",
            scheduling_lanes=["LIVE"],
        )
    )

    assert db.executed
    backfill_sql = db.executed[0][0]
    assert "job_type='decompose_claim_frames'" in backfill_sql
    assert "partition_key='timeline:'" in backfill_sql

    assert "q.job_type <> 'decompose_claim_frames'" in db.claim_sql
    assert "active.job_type='decompose_claim_frames'" in db.claim_sql
    assert "active.partition_key=q.partition_key" in db.claim_sql
