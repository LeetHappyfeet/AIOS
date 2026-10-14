"""Quarantine phases share one evidence-safe implementation but separate SQL budgets."""
import asyncio
from types import SimpleNamespace

import pytest

from aios_app.semantic_index.eligibility import (
    QUARANTINE_PHASES, quarantine_ineligible_vectors_once,
)


class RecordingDatabase:
    def __init__(self):
        self.executes = []
        self.fetches = []

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "UPDATE 0"

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return []


def _cfg():
    return SimpleNamespace(
        batch_size=4,
        proposition_collection="propositions_v1",
        epistemic_collection="epistemic_objects_v1",
    )


@pytest.mark.parametrize("phase", [
    "cluster_candidates", "event_memberships", "events",
    "episode_memberships", "topology_nodes",
])
def test_derived_quarantine_phase_executes_only_one_statement(phase):
    db = RecordingDatabase()
    count = asyncio.run(quarantine_ineligible_vectors_once(db, _cfg(), phase=phase))
    assert count == 0
    assert len(db.executes) == 1, phase
    assert not db.fetches, phase


def test_vector_quarantine_phase_does_not_run_global_derived_scans():
    db = RecordingDatabase()
    count = asyncio.run(quarantine_ineligible_vectors_once(db, _cfg(), phase="vectors"))
    assert count == 0
    assert not db.executes
    assert len(db.fetches) == 1
    assert "semantic_vector_index_state" in db.fetches[0][0]


def test_unphased_legacy_quarantine_still_runs_all_maintenance_phases():
    db = RecordingDatabase()
    count = asyncio.run(quarantine_ineligible_vectors_once(db, _cfg()))
    assert count == 0
    assert len(db.executes) == 5
    assert len(db.fetches) == 1
    assert set(QUARANTINE_PHASES) == {
        "cluster_candidates", "event_memberships", "events",
        "episode_memberships", "topology_nodes", "vectors",
    }


def test_unknown_phase_is_rejected_without_database_mutation():
    db = RecordingDatabase()
    with pytest.raises(ValueError, match="unsupported quarantine phase"):
        asyncio.run(quarantine_ineligible_vectors_once(db, _cfg(), phase="unknown"))
    assert not db.executes and not db.fetches
