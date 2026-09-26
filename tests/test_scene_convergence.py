from uuid import uuid4

import pytest

from aios_app.epistemic.scene_state import CharacterSceneStateStore, PROJECTION_VERSION


class ReadDB:
    def __init__(self,row=None):
        self.row=row
        self.executed=False
    async def fetchrow(self,sql,*args):
        return self.row
    async def execute(self,*args):
        self.executed=True
        raise AssertionError("scene current() must be read only")


@pytest.mark.asyncio
async def test_scene_v2_current_is_read_only():
    sid=uuid4()
    db=ReadDB({"snapshot_id":sid,"scene_state":{"pending_work":{"kind":"action"}},
               "projection_version":PROJECTION_VERSION})
    state=await CharacterSceneStateStore(db).current(
        instance_id=uuid4(),runtime_timeline_id=uuid4(),runtime_head_node_id=None,
        source_timeline_id=None,source_head_node_id=None)
    assert state["snapshot_id"]==sid
    assert state["pending_work"]["kind"]=="action"
    assert db.executed is False


def test_scene_projection_version_is_v2():
    assert PROJECTION_VERSION=="character-scene-v2"
