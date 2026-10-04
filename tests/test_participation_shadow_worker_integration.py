"""Participation comparison coverage is explicit; a zero-row ledger is not a V4 result."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from aios_app.agent.participation import ParticipationService


class EmptyExperimentDB:
    def __init__(self, *, expired: bool):
        self.experiment_id = uuid4()
        self.instance_id = uuid4()
        self.expired = expired

    async def fetchrow(self, sql, *args):
        if "character_participation_worker_heartbeat" in sql:
            return {"ready": not self.expired, "worker_id": "fixture",
                    "last_seen_at": datetime.now(timezone.utc),
                    "last_error": None}
        if "character_participation_experiment" in sql:
            return {
                "experiment_id": self.experiment_id,
                "instance_id": self.instance_id,
                "status": "running",
                "until_at": datetime.now(timezone.utc) + timedelta(
                    seconds=-5 if self.expired else 300
                ),
                "enqueued_count": 1,
            }
        if "COUNT(*) AS evaluated" in sql:
            return {"evaluated": 0, "v3_compared": 0, "v4_compared": 0}
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "character_participation_pending" in sql:
            return [{"status": "pending", "count": 1}]
        return []


def test_empty_v4_comparison_is_marked_no_evaluation_not_success():
    db = EmptyExperimentDB(expired=False)
    result = asyncio.run(
        ParticipationService(db).inspect(db.instance_id, db.experiment_id)
    )
    coverage = result["evaluation_coverage"]
    assert coverage["evaluated"] == 0
    assert coverage["v3_compared"] == 0
    assert coverage["v4_compared"] == 0
    assert coverage["state"] == "queued"
    assert coverage["expected"] == 1
    assert coverage["worker"]["ready"] is True
    assert coverage["worker_required"] is True
    assert coverage["participation_live_admission"] is False
    assert "not a V3/V4 result" in coverage["note"]


def test_expired_unevaluated_experiment_requires_new_run():
    db = EmptyExperimentDB(expired=True)
    result = asyncio.run(
        ParticipationService(db).inspect(db.instance_id, db.experiment_id)
    )
    assert "expired" in result["evaluation_coverage"]["note"]
    assert result["evaluation_coverage"]["state"] == "expired_incomplete"


def test_launch_opt_in_does_not_make_shadow_policy_authoritative():
    from pathlib import Path
    source = Path("launch.py").read_text()
    worker = Path("agent/participation.py").read_text()
    assert "AIOS_PARTICIPATION_SHADOW_ENABLED" in source
    assert '"required": False' in source
    assert "aios_app.agent.participation" in source
    assert "AIOS_READY service=participation_shadow" in worker
    assert 'result["signals"]["comparison_v4"] = v4' in worker
    assert '"participation-shadow-v1"' in worker


def test_worker_registration_is_required_before_enrollment():
    # Start enforces the shared-DB heartbeat, not the API process env flag.
    import inspect
    start = inspect.getsource(ParticipationService.start)
    assert "await self._worker_status(con)" in start
    assert "Participation shadow evaluator unavailable" in start


def test_incomplete_comparisons_are_not_reported_as_complete():
    db = EmptyExperimentDB(expired=False)
    result = asyncio.run(
        ParticipationService(db).inspect(db.instance_id, db.experiment_id)
    )
    assert result["evaluation_coverage"]["evaluated"] == 0
    assert result["evaluation_coverage"]["state"] != "complete"
