"""Regression checks for starvation-safe semantic maintenance."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from aios_app.semantic_index import cli, service
from aios_app.semantic_index.config import SemanticIndexConfig


def test_phased_quarantine_uses_adaptive_batch_budget():
    mapping = {"topology-quarantine": "background_batch_size"}
    assert cli._topology_batch_key("topology-quarantine/vectors", mapping) == "background_batch_size"
    assert cli._topology_batch_key("topology-quarantine/events", mapping) == "background_batch_size"
    assert cli._topology_batch_key("neighbors", mapping) is None


def test_snapshot_discloses_degraded_stages_instead_of_claiming_convergence():
    cfg = SemanticIndexConfig()
    totals = {key: 0 for key in (
        "source_indexed", "frames_indexed", "propositions_indexed", "epistemic_indexed",
        "admitted", "admission_bypassed", "structured", "neighbor_classified",
        "clustered", "classified", "reconciled"
    )}
    snap = cli._semantic_snapshot(
        state="DEGRADED", stage="retry", stage_started_at=None,
        cfg=cfg, last_batches={}, totals=totals, window_started=cli.time.monotonic(),
        failed_stages=["vector-propositions"],
        failure_streaks={"vector-propositions": 3},
    )
    assert not snap["converged"]
    assert snap["failed_stages"] == ["vector-propositions"]
    assert snap["failure_streaks"]["vector-propositions"] == 3


@pytest.mark.asyncio
async def test_ineligible_proposition_page_advances_then_wraps():
    cfg = SemanticIndexConfig()
    key = (cfg.proposition_collection, cfg.embedding_model, cfg.embedding_version)
    service._PROPOSITION_SCAN_CURSORS.pop(key, None)
    pid = uuid4()
    created = datetime.now(timezone.utc)
    class DB:
        def __init__(self):
            self.args = []
        async def fetch(self, sql, *args):
            assert "WITH pending AS MATERIALIZED" in sql
            assert "AS topology_eligible" in sql
            self.args.append(args)
            return ([{"proposition_id": pid, "created_at": created, "topology_eligible": False}]
                    if len(self.args) == 1 else [])
    db = DB()
    try:
        assert await service.index_propositions_once(db, cfg) == 0
        assert service._PROPOSITION_SCAN_CURSORS[key] == (created, pid)
        assert await service.index_propositions_once(db, cfg) == 0
        assert db.args[1][-2:] == (created, pid)
        assert key not in service._PROPOSITION_SCAN_CURSORS
    finally:
        service._PROPOSITION_SCAN_CURSORS.pop(key, None)


@pytest.mark.asyncio
async def test_failed_discovery_does_not_advance_scan_cursor():
    cfg = SemanticIndexConfig()
    key = (cfg.proposition_collection, cfg.embedding_model, cfg.embedding_version)
    service._PROPOSITION_SCAN_CURSORS.pop(key, None)
    class DB:
        async def fetch(self, sql, *args):
            raise TimeoutError("SQL budget")
    with pytest.raises(TimeoutError):
        await service.index_propositions_once(DB(), cfg)
    assert key not in service._PROPOSITION_SCAN_CURSORS
