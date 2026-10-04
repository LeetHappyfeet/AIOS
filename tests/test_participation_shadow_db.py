"""Optional disposable-Postgres test: install pgserver and asyncpg to run."""
import asyncio
from pathlib import Path
import re
import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from aios_app.agent.participation import ParticipationService


def test_disposable_postgres_queue_and_worker():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("Disposable PostgreSQL test must run as an unprivileged user")
    pgserver = pytest.importorskip("pgserver")
    pytest.importorskip("asyncpg")
    from aios_app.db import Database
    server = pgserver.get_server(Path(tempfile.mkdtemp(prefix="aios-shadow-test-")) / "pgdata",
                                 cleanup_mode="delete")
    root = Path(__file__).resolve().parents[1]

    async def check():
        db = Database(server.get_uri(), max_size=4)
        await db.connect()
        try:
            # Use actual table definitions, omitting unrelated foreign keys.
            baseline = (root / "aios_baseline.sql").read_text()
            keys = {
                "character_identity": "character_id", "character_instance": "instance_id",
                "character_knowledge": "instance_id,claim_id", "claim_context_resolution": "claim_id",
                "observation": "observation_id", "proposition": "proposition_id",
                "proposition_conflict": "conflict_id", "world_entity": "entity_id",
                "character_relationship": "relationship_id", "claim_candidate": "claim_id",
            }
            await db.execute("CREATE SCHEMA aios")
            for table, key in keys.items():
                definition = re.search(r"CREATE TABLE aios\." + table + r" \(.*?\n\);", baseline, re.S)[0]
                await db.execute(definition)
                await db.execute(f"ALTER TABLE aios.{table} ADD PRIMARY KEY ({key})")
            await db.execute("ALTER TABLE aios.observation ADD UNIQUE(claim_id)")
            for filename, table in [
                ("20260919_identity_kernel.sql", "character_identity_facet"),
                ("20260919_identity_kernel.sql", "character_identity_candidate"),
                ("20260923_14_cognitive_actions.sql", "character_agent_goal"),
            ]:
                source = (root / "migrations/current" / filename).read_text()
                definition = re.search(r"CREATE TABLE IF NOT EXISTS aios\." + table + r" \(.*?\n\);", source, re.S)[0]
                definition = re.sub(r"REFERENCES aios\.\w+\(\w+\) ON DELETE (CASCADE|SET NULL)", "", definition)
                await db.execute(definition)
            migration = (root / "migrations/current/20260930_02_shadow_participation.sql").read_text()
            await db.execute(migration)
            await db.execute(migration)  # migration can be safely reapplied
            heartbeat_migration = (
                root / "migrations/current/20261003_14_participation_worker_heartbeat.sql"
            ).read_text()
            await db.execute(heartbeat_migration)
            # The actual worker populates this on startup; an explicit fixture
            # heartbeat is required for this scoped disposable integration run.
            await db.execute(
                """INSERT INTO aios.character_participation_worker_heartbeat
                   (worker_name,worker_id,policy_version)
                   VALUES('participation_shadow','fixture','participation-shadow-v1')"""
            )
            instance, other = uuid4(), uuid4()
            world = uuid4()
            await db.execute("INSERT INTO aios.character_identity(character_id,display_name) VALUES('renamon','Renamon')")
            await db.execute("INSERT INTO aios.character_instance(instance_id,character_id,world_id) VALUES($1,'renamon',$3),($2,'renamon',$3)", instance, other, world)
            service = ParticipationService(db)
            exp = await service.start(instance, max_claims=2)
            exp_id = exp["experiment_id"]
            claims = [uuid4() for _ in range(4)]
            # Knowledge arrives before observation/context. This must enqueue.
            await db.execute("INSERT INTO aios.character_knowledge(instance_id,claim_id) VALUES($1,$2)", instance, claims[0])
            await db.execute("UPDATE aios.character_knowledge SET updated_at=now() WHERE instance_id=$1 AND claim_id=$2", instance, claims[0])
            await db.execute("INSERT INTO aios.character_knowledge(instance_id,claim_id) VALUES($1,$2)", other, claims[3])
            assert await service.process_pending() == 0  # unresolved evidence defers
            async def acquire(cid):
                await db.execute("INSERT INTO aios.character_knowledge(instance_id,claim_id) VALUES($1,$2)", instance, cid)
            await asyncio.gather(acquire(claims[1]), acquire(claims[2]))
            report = await service.inspect(instance, exp_id)
            assert report["experiment"]["enqueued_count"] == 2
            assert report["queue"] == [{"status": "pending", "count": 2}]
            prop = uuid4()
            await db.execute("""INSERT INTO aios.proposition(proposition_id,proposition_hash,topic_key,canonical_text,subject_norm,object_norm,atom_id)
                VALUES($1,'test-hash','test','Renamon protects autonomy','renamon','autonomy',$2)""", prop, uuid4())
            queued = await db.fetch("SELECT claim_id FROM aios.character_participation_pending WHERE experiment_id=$1", exp_id)
            for row in queued:
                await db.execute("INSERT INTO aios.observation(claim_id,proposition_id) VALUES($1,$2)", row["claim_id"], prop)
                await db.execute("INSERT INTO aios.claim_context_resolution(claim_id,claim_kind,resolver_version) VALUES($1,'FACT','test')", row["claim_id"])
            await db.execute("UPDATE aios.character_participation_pending SET ready_at=now() WHERE experiment_id=$1", exp_id)
            assert await service.process_pending() == 2
            assert await service.process_pending() == 0
            report = await service.inspect(instance, exp_id)
            assert report["proposed_foreground_share"] == 1.0
            assert len(report["evaluations"]) == 2
            assert report["evaluations"][0]["claim_snapshot"]["epistemic_status"] == "observed"
            assert report["evaluations"][0]["signals"]["direct_involvement"]
            assert report["comparisons"][0]["evaluation_mode"] == "paired_live"
            with pytest.raises(LookupError):
                await service.inspect(other, exp_id)
            await service.stop(instance, exp_id)
            assert await service.process_pending() == 0
            # Backfill is also capped and never enrolls a sibling's knowledge.
            backfill = await service.start(instance, since_at=datetime.now(timezone.utc)-timedelta(minutes=5), max_claims=1)
            assert backfill["enqueued_count"] == 1
            assert await service.process_pending(limit=1) in (0, 1)
        finally:
            await db.close()
    try:
        asyncio.run(check())
    finally:
        server.cleanup()
