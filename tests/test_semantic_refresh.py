import asyncio
from datetime import datetime, timedelta, timezone

from aios_app.semantic_index.clustering import cluster_neighbors_once
from aios_app.semantic_index.config import SemanticIndexConfig


class DB:
    def __init__(self, *, changed=True, age=100):
        self.current = datetime.now(timezone.utc)
        self.previous = self.current - timedelta(seconds=1) if changed else self.current
        self.age = age
        self.writes = []
        self.reads = []
    async def execute(self, sql, *args): self.writes.append(sql)
    async def fetchrow(self, sql, *args):
        if 'MAX(changed_at)' in sql:
            return {"watermark":self.current}
        return {"structure_watermark":self.previous,"age_seconds":self.age}
    async def fetch(self, sql, *args):
        self.reads.append(sql)
        return []


def test_changed_relation_watermark_rebuilds_without_new_geometry():
    db = DB()
    asyncio.run(cluster_neighbors_once(db,SemanticIndexConfig()))
    assert any('INSERT INTO aios.semantic_cluster_run' in sql for sql in db.writes)


def test_unchanged_watermark_does_not_rebuild():
    db = DB(changed=False)
    asyncio.run(cluster_neighbors_once(db,SemanticIndexConfig()))
    assert not db.reads
    assert not any('INSERT INTO' in sql for sql in db.writes)


def test_bursts_are_coalesced_before_global_rebuild():
    db = DB(age=1)
    asyncio.run(cluster_neighbors_once(db,SemanticIndexConfig()))
    assert not db.reads
    assert not any('INSERT INTO' in sql for sql in db.writes)
