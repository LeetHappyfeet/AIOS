"""Regression coverage for progress-safe expensive discovery scans."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aios_app import supervisor
from aios_app.semantic_index import eligibility


def test_all_costly_supervisor_stage_filters_match_once():
    for stage_name, (page_sql, marker, replacement) in supervisor._DISCOVERY_PAGES.items():
        stage = next(s for s in supervisor.STAGES if s.name == stage_name)
        assert stage.eligibility_sql.count(marker) == 1, stage_name
        assert "ANY($2::uuid[])" in replacement
        assert "ORDER BY" in page_sql
        assert "LIMIT $2" in page_sql


@pytest.mark.asyncio
async def test_supervisor_advances_cursor_across_empty_eligibility_pages(monkeypatch):
    stage = next(s for s in supervisor.STAGES if s.name == "rdf_epistemic_project")
    supervisor._DISCOVERY_CURSOR.pop(stage.name, None)
    supervisor._DISCOVERY_PAGE_SIZE.pop(stage.name, None)
    candidates = [uuid4(), uuid4()]
    fetches = []
    class DB:
        async def fetch_bounded(self, sql, *args, timeout_seconds):
            fetches.append((sql, args))
            if "SELECT claim_id FROM aios.observation" in sql:
                return ([{"claim_id": candidates[0]}] if args[0] is None
                        else [{"claim_id": candidates[1]}] if args[0] == candidates[0]
                        else [])
            assert "o.claim_id=ANY($2::uuid[])" in sql
            return []
    try:
        assert await supervisor.enqueue_stage_jobs(DB(), stage, batch_size=8) == 0
        assert supervisor._DISCOVERY_CURSOR[stage.name] == candidates[0]
        assert await supervisor.enqueue_stage_jobs(DB(), stage, batch_size=8) == 0
        assert supervisor._DISCOVERY_CURSOR[stage.name] == candidates[1]
        assert await supervisor.enqueue_stage_jobs(DB(), stage, batch_size=8) == 0
        assert stage.name not in supervisor._DISCOVERY_CURSOR
        assert len(fetches) == 5
    finally:
        supervisor._DISCOVERY_CURSOR.pop(stage.name, None)
        supervisor._DISCOVERY_PAGE_SIZE.pop(stage.name, None)


@pytest.mark.asyncio
async def test_supervisor_keeps_cursor_on_failed_eligibility(monkeypatch):
    stage = next(s for s in supervisor.STAGES if s.name == "derive_claim_topology")
    supervisor._DISCOVERY_CURSOR.pop(stage.name, None)
    supervisor._DISCOVERY_PAGE_SIZE.pop(stage.name, None)
    key = uuid4()
    class DB:
        async def fetch_bounded(self, sql, *args, timeout_seconds):
            if "SELECT claim_id FROM aios.observation" in sql:
                return [{"claim_id": key}]
            raise asyncio.TimeoutError()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await supervisor.enqueue_stage_jobs(DB(), stage, batch_size=8)
        assert stage.name not in supervisor._DISCOVERY_CURSOR
        assert supervisor._DISCOVERY_PAGE_SIZE[stage.name] == 32
    finally:
        supervisor._DISCOVERY_CURSOR.pop(stage.name, None)
        supervisor._DISCOVERY_PAGE_SIZE.pop(stage.name, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["event_memberships", "topology_nodes", "vectors"])
async def test_quarantine_candidate_page_advances_on_success(phase):
    cfg = SimpleNamespace(
        batch_size=1, proposition_collection="props", epistemic_collection="epistemic"
    )
    eligibility._QUARANTINE_CURSORS.pop(phase, None)
    eligibility._QUARANTINE_PAGE_SIZES.pop(phase, None)
    key = str(uuid4()) if phase == "vectors" else uuid4()
    class DB:
        def __init__(self):
            self.calls = []
        async def fetch(self, sql, *args):
            self.calls.append(("fetch", sql, args))
            if "AS page_key" in sql:
                return [{"page_key": key}]
            return []
        async def execute(self, sql, *args):
            self.calls.append(("execute", sql, args))
            return "UPDATE 0"
    db = DB()
    try:
        assert await eligibility.quarantine_ineligible_vectors_once(db, cfg, phase=phase) == 0
        assert eligibility._QUARANTINE_CURSORS[phase] == key
        assert any("ANY($2::uuid[])" in sql or "ANY($3::text[])" in sql
                   for _,sql,_ in db.calls)
    finally:
        eligibility._QUARANTINE_CURSORS.pop(phase, None)
        eligibility._QUARANTINE_PAGE_SIZES.pop(phase, None)


@pytest.mark.asyncio
async def test_topology_node_page_predicate_scopes_every_or_arm():
    cfg = SimpleNamespace(batch_size=2, proposition_collection="props",
                          epistemic_collection="epistemic")
    key = uuid4()
    class DB:
        async def fetch(self, sql, *args):
            return [{"page_key": key}]
        async def execute(self, sql, *args):
            # The page restriction must govern every OR branch, including
            # NARRATED_SCENE_EVENT and semantic_event retractions.
            assert "WHERE x.topology_node_id=ANY($2::uuid[]) AND ((" in sql
            assert ") ORDER BY x.topology_node_id LIMIT $1" in sql
            assert args[1] == [key]
            return "DELETE 0"
    try:
        await eligibility.quarantine_ineligible_vectors_once(DB(), cfg,
                                                             phase="topology_nodes")
    finally:
        eligibility._QUARANTINE_CURSORS.pop("topology_nodes", None)
        eligibility._QUARANTINE_PAGE_SIZES.pop("topology_nodes", None)
