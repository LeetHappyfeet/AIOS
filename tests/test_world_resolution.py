from uuid import uuid4

import pytest

from aios_app.world.resolution import (
    COMMON_WORLD_DOMAINS,
    get_effective_world_ids,
    normalize_domain,
    set_domain_policy,
    set_world_relation,
)


class FakeDB:
    def __init__(self):
        self.world_id = uuid4()
        self.parent_id = uuid4()
        self.relation_id = uuid4()
        self.fetch_sql = None
        self.fetch_args = None
        self.returning_args = None
        self.execute_args = None

    async def fetch(self, sql, *args):
        self.fetch_sql = sql
        self.fetch_args = args
        return [
            {"source_world_id": self.world_id, "priority": 0, "depth": 0},
            {"source_world_id": self.parent_id, "priority": 100100, "depth": 1},
        ]

    async def execute_returning_row(self, sql, *args):
        assert "world_relation" in sql
        self.returning_args = args
        return {"relation_id": self.relation_id}

    async def execute(self, sql, *args):
        assert "world_domain_policy" in sql
        self.execute_args = args
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_live_scope_is_flat_cache_lookup_not_recursive_reasoning():
    db = FakeDB()
    ids = await get_effective_world_ids(db, world_id=db.world_id, domain="Geography")
    assert ids == (db.world_id, db.parent_id)
    assert "get_world_resolution_scope" in db.fetch_sql
    assert "WITH RECURSIVE" not in db.fetch_sql.upper()
    assert db.fetch_args == (db.world_id, "geography")


@pytest.mark.asyncio
async def test_relation_materializes_only_explicit_domains():
    db = FakeDB()
    relation_id = await set_world_relation(
        db,
        world_id=db.world_id,
        source_world_id=db.parent_id,
        domains=["geography", "biology", "chemistry"],
        relation="counterpart_of",
    )
    assert relation_id == db.relation_id
    assert db.returning_args[0] == db.world_id
    assert db.returning_args[1] == db.parent_id
    assert db.returning_args[2] == "counterpart_of"
    assert db.returning_args[4] == ["geography", "biology", "chemistry"]


@pytest.mark.asyncio
async def test_local_only_is_explicit_canon_boundary():
    db = FakeDB()
    await set_domain_policy(
        db,
        world_id=db.world_id,
        domain="history",
        inheritance_mode="local_only",
    )
    assert db.execute_args[0] == db.world_id
    assert db.execute_args[1] == "history"
    assert db.execute_args[2] == "local_only"


def test_domains_are_bounded_and_normalized():
    assert "physics" in COMMON_WORLD_DOMAINS
    assert normalize_domain("  Geography ") == "geography"
    with pytest.raises(ValueError):
        normalize_domain("historical events / alternate canon")
