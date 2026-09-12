from uuid import uuid4

import pytest

from aios_app.world.timeline import (
    EXPOSURE_KINDS,
    ensure_objective_timeline,
    record_world_event_exposure,
)


class FakeDB:
    def __init__(self):
        self.world_id = uuid4()
        self.world_timeline_id = uuid4()
        self.exposure_id = uuid4()
        self.returning_args = None

    async def fetchrow(self, sql, *args):
        if "ensure_world_objective_timeline" in sql:
            return {"timeline_id": self.world_timeline_id}
        if "character_world_timeline_link" in sql:
            return {
                "character_timeline_id": args[0],
                "world_id": self.world_id,
                "world_timeline_id": self.world_timeline_id,
                "relation": "inhabits",
                "created_at": None,
                "meta": {},
            }
        raise AssertionError(sql)

    async def execute_returning_row(self, sql, *args):
        assert "world_event_exposure" in sql
        self.returning_args = args
        return {"exposure_id": self.exposure_id}


@pytest.mark.asyncio
async def test_ensure_objective_timeline_uses_database_invariant():
    db = FakeDB()
    assert await ensure_objective_timeline(db, world_id=db.world_id) == db.world_timeline_id


@pytest.mark.asyncio
async def test_exposure_uses_linked_world_coordinate_not_character_copy():
    db = FakeDB()
    instance_id = uuid4()
    character_timeline_id = uuid4()
    world_node_id = uuid4()

    exposure_id = await record_world_event_exposure(
        db,
        instance_id=instance_id,
        character_timeline_id=character_timeline_id,
        world_node_id=world_node_id,
        exposure_kind="observed",
        confidence=0.9,
    )

    assert exposure_id == db.exposure_id
    assert db.returning_args[0] == instance_id
    assert db.returning_args[1] == db.world_id
    assert db.returning_args[2] == db.world_timeline_id
    assert db.returning_args[3] == world_node_id
    assert db.returning_args[4] == character_timeline_id


def test_exposure_kinds_are_explicit_epistemic_bridges():
    assert {"observed", "heard_about", "inferred_from", "sensor"} <= EXPOSURE_KINDS


@pytest.mark.asyncio
async def test_exposure_rejects_invalid_kind_and_confidence():
    db = FakeDB()
    with pytest.raises(ValueError, match="unsupported exposure_kind"):
        await record_world_event_exposure(
            db,
            instance_id=uuid4(),
            character_timeline_id=uuid4(),
            world_node_id=uuid4(),
            exposure_kind="teleported_knowledge",
        )

    with pytest.raises(ValueError, match="confidence"):
        await record_world_event_exposure(
            db,
            instance_id=uuid4(),
            character_timeline_id=uuid4(),
            world_node_id=uuid4(),
            exposure_kind="observed",
            confidence=1.1,
        )
