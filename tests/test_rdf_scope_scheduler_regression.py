"""RDF scope publication and scheduler regression guards.

The schema guard is intentionally checked against both the baseline and the
deployment migration: a dedicated scope index alone cannot override an older
generic singleton index.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aios_app.pipeline.job_registry import ResourceClass, SchedulingLane
from aios_app.runner import _claim_for_worker
from aios_app.epistemic import topology_projection as projection


def test_global_singleton_excludes_scope_key_in_baseline_and_migration():
    baseline = Path("aios_baseline.sql").read_text()
    index = baseline.split(
        "CREATE UNIQUE INDEX ux_pipeline_job_global_active", 1
    )[1].split(";", 1)[0]
    assert "NOT (payload ? 'scope_key'::text)" in index
    scope = baseline.split(
        "CREATE UNIQUE INDEX ux_pipeline_project_semantic_scope_active", 1
    )[1].split(";", 1)[0]
    assert "payload ->> 'scope_key'" in scope
    migration = Path(
        "migrations/current/20261003_02_rdf_scope_job_uniqueness.sql"
    ).read_text()
    assert "NOT (payload ? 'scope_key')" in migration


@pytest.mark.asyncio
async def test_rdf_background_reservation_precedes_default_claim():
    background = {"job_id": "background"}
    calls = []

    async def fetch(_db, **kwargs):
        calls.append(kwargs)
        if kwargs["scheduling_lanes"] == [SchedulingLane.BACKGROUND.value]:
            return background
        return {"job_id": "default"}

    with patch("aios_app.runner.fetch_next_job", side_effect=fetch):
        selected = await _claim_for_worker(
            object(), worker_id="rdf-test",
            resource_class=ResourceClass.RDF, worker_index=0,
            claim_gate=asyncio.Lock(), reserve_rdf_background=True,
        )
    assert selected == background
    assert len(calls) == 1
    assert calls[0]["prefer_foreground"] is False


@pytest.mark.asyncio
async def test_rdf_reservation_falls_back_to_default_when_background_empty():
    calls = []

    async def fetch(_db, **kwargs):
        calls.append(kwargs["scheduling_lanes"])
        if kwargs["scheduling_lanes"] == [SchedulingLane.BACKGROUND.value]:
            return None
        return {"job_id": "default"}

    with patch("aios_app.runner.fetch_next_job", side_effect=fetch):
        selected = await _claim_for_worker(
            object(), worker_id="rdf-test",
            resource_class=ResourceClass.RDF, worker_index=0,
            claim_gate=asyncio.Lock(), reserve_rdf_background=True,
        )
    assert selected["job_id"] == "default"
    assert calls == [[SchedulingLane.BACKGROUND.value], None]


def test_hot_scope_window_is_consistent_across_select_and_enqueue():
    source = Path("epistemic/topology_projection.py").read_text()
    assert "RDF_PROJECTION_MAX_AGE_SECONDS = 300.0" in source
    assert "COALESCE(first_dirty_at, dirty_at)" in source
    assert "COALESCE(s.first_dirty_at, s.dirty_at)" in source
    assert "first_dirty_at=CASE" in source
    migration = Path("migrations/current/20261003_03_rdf_scope_dirty_window.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS first_dirty_at" in migration


@pytest.mark.asyncio
async def test_unchanged_authority_does_not_issue_second_fuseki_update():
    from aios_app.rdf.epistemic_writer import _project_observation_authority

    class ReceiptDb:
        def __init__(self):
            self.fingerprint = None
            self.acks = 0

        async def fetchrow(self, sql, *args):
            return (
                {"projection_hash": self.fingerprint}
                if self.fingerprint is not None else None
            )

        async def execute(self, sql, *args):
            self.fingerprint = args[3]
            self.acks += 1

    class Fuseki:
        def __init__(self):
            self.writes = []

        def update(self, dataset, sparql):
            self.writes.append((dataset, sparql))

    authority = {
        "origin_kind": "self_utterance",
        "epistemic_mode": "subjective",
        "authority_state": "admitted",
        "authority_rank": 1,
        "lineage_key": "source:test",
        "predicate_class": "observation",
        "policy_version": "test-v1",
        "authorized_uses": ["character_memory"],
    }
    db, fuseki = ReceiptDb(), Fuseki()
    args = dict(
        dataset="char", graph_iri="urn:test:char",
        observation_iri="urn:test:observation",
        prefix="char", namespace="urn:aios:char#", authority=authority,
    )
    await _project_observation_authority(db, fuseki, **args)
    await _project_observation_authority(db, fuseki, **args)
    assert len(fuseki.writes) == 1
    assert db.acks == 1
    await _project_observation_authority(
        db, fuseki, **{**args, "authority": {**authority, "authority_state": "revised"}}
    )
    assert len(fuseki.writes) == 2
    assert db.acks == 2


def test_all_authority_projection_calls_supply_db_and_fuseki():
    """Guard both /world and /char branches against argument drift."""
    import ast
    source = Path("rdf/epistemic_writer.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    calls = [
        node for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_project_observation_authority"
    ]
    assert len(calls) == 2
    for call in calls:
        assert len(call.args) == 2
        assert [arg.id for arg in call.args] == ["db", "fuseki"]
