from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from aios_app.world.source_cursor import advance_matching_runtime_source_cursor
from aios_app.world.topology import latest_source_anchor


class CursorConnection:
    def __init__(self, *, source, runtime_rows):
        self.source = source
        self.runtime_rows = runtime_rows
        self.executed = []

    def transaction(self):
        @asynccontextmanager
        async def _transaction():
            yield self

        return _transaction()

    async def fetchrow(self, sql, *args):
        if "FROM aios.timeline t" in sql and "JOIN aios.dag_node dn" in sql:
            return self.source
        raise AssertionError(f"unexpected fetchrow query: {sql}")

    async def fetch(self, sql, *args):
        if "FROM aios.character_runtime_state rs" in sql:
            return self.runtime_rows
        raise AssertionError(f"unexpected fetch query: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class CursorDB:
    def __init__(self, connection):
        self._connection = connection

    @asynccontextmanager
    async def connection(self):
        yield self._connection


@pytest.mark.asyncio
async def test_unbound_runtime_adopts_first_exact_liminal_source():
    instance_id = uuid4()
    runtime_timeline_id = uuid4()
    source_timeline_id = uuid4()
    source_node_id = uuid4()
    session_id = uuid4()

    con = CursorConnection(
        source={
            "timeline_id": source_timeline_id,
            "session_id": session_id,
            "character_id": "Kernel_Test_Bob",
            "user_name": "Ren-119",
            "scope_key": "conversation",
            "world_key": "liminal",
            "node_id": source_node_id,
            "event_id": 1368,
        },
        runtime_rows=[
            {
                "instance_id": instance_id,
                "runtime_timeline_id": runtime_timeline_id,
                "source_timeline_id": None,
                "source_head_node_id": None,
                "current_source_event_id": None,
                "current_source_world_key": None,
            }
        ],
    )

    advanced = await advance_matching_runtime_source_cursor(
        CursorDB(con),
        character_id="Kernel_Test_Bob",
        session_id=session_id,
        user_name="Ren-119",
        scope_key="conversation",
        source_timeline_id=source_timeline_id,
        source_head_node_id=source_node_id,
        source_head_event_id=1368,
    )

    assert advanced == [instance_id]
    assert len(con.executed) == 1
    sql, args = con.executed[0]
    assert "UPDATE aios.character_runtime_state" in sql
    assert args[1] == source_timeline_id
    assert args[2] == source_node_id
    assert all("UPDATE aios.world" not in statement for statement, _ in con.executed)


@pytest.mark.asyncio
async def test_runtime_never_silently_rebinds_between_liminal_sources():
    instance_id = uuid4()
    runtime_timeline_id = uuid4()
    existing_source_timeline_id = uuid4()
    incoming_source_timeline_id = uuid4()
    source_node_id = uuid4()
    session_id = uuid4()

    con = CursorConnection(
        source={
            "timeline_id": incoming_source_timeline_id,
            "session_id": session_id,
            "character_id": "Kernel_Test_Bob",
            "user_name": "Ren-119",
            "scope_key": "conversation",
            "world_key": "liminal",
            "node_id": source_node_id,
            "event_id": 1400,
        },
        runtime_rows=[
            {
                "instance_id": instance_id,
                "runtime_timeline_id": runtime_timeline_id,
                "source_timeline_id": existing_source_timeline_id,
                "source_head_node_id": uuid4(),
                "current_source_event_id": 1399,
                "current_source_world_key": "liminal",
            }
        ],
    )

    advanced = await advance_matching_runtime_source_cursor(
        CursorDB(con),
        character_id="Kernel_Test_Bob",
        session_id=session_id,
        user_name="Ren-119",
        scope_key="conversation",
        source_timeline_id=incoming_source_timeline_id,
        source_head_node_id=source_node_id,
        source_head_event_id=1400,
    )

    assert advanced == []
    assert con.executed == []


@pytest.mark.asyncio
async def test_legacy_runtime_self_binding_is_repaired_on_ingress():
    instance_id = uuid4()
    runtime_timeline_id = uuid4()
    source_timeline_id = uuid4()
    source_node_id = uuid4()
    session_id = uuid4()

    con = CursorConnection(
        source={
            "timeline_id": source_timeline_id,
            "session_id": session_id,
            "character_id": "Kernel_Test_Bob",
            "user_name": "Ren-119",
            "scope_key": "conversation",
            "world_key": "liminal",
            "node_id": source_node_id,
            "event_id": 1368,
        },
        runtime_rows=[
            {
                "instance_id": instance_id,
                "runtime_timeline_id": runtime_timeline_id,
                "source_timeline_id": runtime_timeline_id,
                "source_head_node_id": None,
                "current_source_event_id": None,
                "current_source_world_key": "char:Kernel_Test_Bob:session:test",
            }
        ],
    )

    advanced = await advance_matching_runtime_source_cursor(
        CursorDB(con),
        character_id="Kernel_Test_Bob",
        session_id=session_id,
        user_name="Ren-119",
        scope_key="conversation",
        source_timeline_id=source_timeline_id,
        source_head_node_id=source_node_id,
        source_head_event_id=1368,
    )

    assert advanced == [instance_id]
    assert len(con.executed) == 1


class AnchorDB:
    def __init__(self, row=None):
        self.row = row
        self.sql = None
        self.args = None

    async def fetchrow(self, sql, *args):
        self.sql = sql
        self.args = args
        return self.row


@pytest.mark.asyncio
async def test_latest_source_anchor_requires_exact_liminal_runtime_identity():
    db = AnchorDB()
    session_id = uuid4()

    result = await latest_source_anchor(
        db,
        character_id="Kernel_Test_Bob",
        session_id=session_id,
    )

    assert result == (None, None)
    assert "rw.world_key <> 'liminal'" in db.sql
    assert "COALESCE(rt.meta->>'world_runtime','false')='true'" in db.sql
    assert "w.world_key='liminal'" in db.sql
    assert "t.user_name IS NOT DISTINCT FROM i.user_name" in db.sql
    assert "t.scope_key IS NOT DISTINCT FROM i.scope_key" in db.sql
    assert "count(*) FROM candidates)=1" in db.sql
    assert db.args[:2] == (session_id, "Kernel_Test_Bob")


@pytest.mark.asyncio
async def test_latest_source_anchor_accepts_explicit_user_and_scope_identity():
    source_timeline_id = uuid4()
    source_node_id = uuid4()
    session_id = uuid4()
    db = AnchorDB({"timeline_id": source_timeline_id, "node_id": source_node_id})

    result = await latest_source_anchor(
        db,
        character_id="Kernel_Test_Bob",
        session_id=session_id,
        user_name="Ren-119",
        scope_key="conversation",
    )

    assert result == (source_timeline_id, source_node_id)
    assert db.args == (
        session_id,
        "Kernel_Test_Bob",
        "Ren-119",
        "conversation",
    )
