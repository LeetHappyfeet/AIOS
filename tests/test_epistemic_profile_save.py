from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from aios_app.epistemic import weights


class FakeDB:
    def __init__(self):
        self.events = []

    @asynccontextmanager
    async def atomic(self):
        self.events.append("begin")
        try:
            yield
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    async def execute(self, sql, *args):
        self.events.append(("execute", args))


def test_profile_save_updates_continuity_and_reweights_in_one_transaction(monkeypatch):
    db = FakeDB()

    async def save(_db, *, character_id, data):
        assert db.events == ["begin"]
        assert character_id == "example"
        assert data["curiosity"] == 0.8
        db.events.append("profile")
        return {"character_id": character_id}

    async def reweight(_db, *, character_id):
        assert character_id == "example"
        db.events.append("reweight")
        return 7

    monkeypatch.setattr(weights, "upsert_profile", save)
    monkeypatch.setattr(weights, "reweight_character_knowledge", reweight)
    profile, count = asyncio.run(weights.save_character_epistemic_profile(
        db, character_id="example", data={"curiosity": 0.8},
        memory_continuity="isolated",
    ))
    assert profile["memory_continuity"] == "isolated"
    assert count == 7
    assert db.events == [
        "begin", "profile", ("execute", ("example", "isolated")),
        "reweight", "commit",
    ]


@pytest.mark.parametrize("data, mode", [
    ({"skepticism": 1.5}, "character"),
    ({"curiosity": float("nan")}, "character"),
    ({"source_trust": {"untrusted": -0.1}}, "character"),
    ({"domain_expertise": {"topic": float("inf")}}, "character"),
    ({"trait_weights": {"trait": 2.0}}, "character"),
    ({}, "invalid"),
])
def test_invalid_profile_is_rejected_before_writes(monkeypatch, data, mode):
    db = FakeDB()
    save = AsyncMock()
    monkeypatch.setattr(weights, "upsert_profile", save)
    with pytest.raises(ValueError):
        asyncio.run(weights.save_character_epistemic_profile(
            db, character_id="example", data=data, memory_continuity=mode,
        ))
    assert db.events == []
    save.assert_not_awaited()


def test_failed_reweight_rolls_back_profile_transaction(monkeypatch):
    db = FakeDB()

    async def save(_db, *, character_id, data):
        db.events.append("profile")
        return {"character_id": character_id}

    async def fail(_db, *, character_id):
        raise RuntimeError("reweight failed")

    monkeypatch.setattr(weights, "upsert_profile", save)
    monkeypatch.setattr(weights, "reweight_character_knowledge", fail)
    with pytest.raises(RuntimeError, match="reweight failed"):
        asyncio.run(weights.save_character_epistemic_profile(
            db, character_id="example", data={},
            memory_continuity="character",
        ))
    assert db.events[-1] == "rollback"
    assert "commit" not in db.events
