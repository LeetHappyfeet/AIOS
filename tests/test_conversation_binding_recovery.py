import asyncio
from uuid import uuid4

from aios_app.world.conversation import bind_available_instance, reconcile_runtime_observations


class BindingDB:
    async def execute_returning_row(self, sql, participant_id):
        # A live conversation must never have its established binding replaced
        # just because another session created a newer character instance.
        assert "cp.character_instance_id IS NULL" in sql
        assert "rt.session_id IS NOT DISTINCT FROM st.session_id" in sql
        assert "rt.user_name IS NOT DISTINCT FROM st.user_name" in sql
        assert "rt.scope_key=st.scope_key" in sql
        assert "rs.source_timeline_id=cp.timeline_id" in sql
        return None


def test_ingestion_preserves_established_binding():
    assert asyncio.run(bind_available_instance(BindingDB(), participant_id=uuid4())) is None


class RecoveryDB:
    def __init__(self, instance_id):
        self.instance_id = instance_id

    async def fetch_bounded(self, sql, instance_id):
        assert instance_id == self.instance_id
        assert "dn.event_id<=head.event_id" in sql
        assert "head.timeline_id=st.timeline_id" in sql
        assert "cp.character_instance_id=rs.instance_id" in sql
        assert "mp.perceived" in sql
        assert "ie.superseded_at IS NULL" in sql
        assert "NOT EXISTS (SELECT 1 FROM aios.knowledge_acquisition_event" in sql
        assert "'hypothetical','conditional','counterfactual','question'" in sql
        assert "LIMIT 128" in sql
        assert "FOR UPDATE OF o SKIP LOCKED" in sql
        return [{"acquisition_id": uuid4()}]


def test_recovery_retains_source_and_modality_boundaries():
    instance_id = uuid4()
    assert asyncio.run(reconcile_runtime_observations(
        RecoveryDB(instance_id), instance_id=instance_id,
    )) == 1
