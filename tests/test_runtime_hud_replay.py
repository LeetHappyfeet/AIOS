from uuid import uuid4

import pytest

from aios_app.world.runtime import WorldRuntimeService


class ReplayDB:
    def __init__(self, *, instance_id, timeline_id, active_head, prepared_head):
        self.instance_id = instance_id
        self.timeline_id = timeline_id
        self.active_head = active_head
        self.prepared_head = prepared_head
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "SELECT rs.source_timeline_id, rs.source_head_node_id, rs.state_version" in sql:
            return {
                "source_timeline_id": self.timeline_id,
                "source_head_node_id": self.active_head,
                "state_version": 7,
                "source_head_event_id": 22,
            }
        if "FROM aios.character_runtime_state rs" in sql and "JOIN aios.character_instance" in sql:
            return {
                "instance_id": self.instance_id,
                "source_timeline_id": self.timeline_id,
                "source_head_node_id": self.active_head,
                "state_version": 7,
            }
        if "SELECT * FROM aios.character_hud_readiness" in sql:
            return {
                "instance_id": self.instance_id,
                "status": "dirty",
                "prepared_source_node_id": self.prepared_head,
                "prepared_state_version": 7,
                "hud_json": '{"hud":{"generation_ready":true,"freshness":{}},"presence":{"state_version":7}}',
            }
        if "FROM aios.dag_node WHERE node_id=" in sql:
            raise AssertionError("replay must not inspect or rebuild from the newer source DAG head")
        raise AssertionError(f"unexpected fetchrow query: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_prepare_frame_replays_exact_previous_generation_snapshot():
    instance_id = uuid4()
    timeline_id = uuid4()
    prepared_user_head = uuid4()
    active_character_head = uuid4()
    db = ReplayDB(
        instance_id=instance_id,
        timeline_id=timeline_id,
        active_head=active_character_head,
        prepared_head=prepared_user_head,
    )
    runtime = WorldRuntimeService(db)

    frame = await runtime.prepare_frame(
        instance_id,
        through_node_id=prepared_user_head,
        wait_ms=0,
    )

    assert frame["hud"]["generation_ready"] is True
    assert frame["hud"]["cache"] == "replayed"
    assert frame["hud"]["freshness"]["replayed_snapshot"] is True
    assert frame["hud"]["freshness"]["requested_source_node_id"] == str(prepared_user_head)
    assert frame["hud"]["freshness"]["active_source_head_node_id"] == str(active_character_head)
