from pathlib import Path


MIGRATION = Path("migrations/current/20260927_coalesced_character_belief_reconciliation.sql")
BELIEF = Path("epistemic/belief_reconciliation.py")
RUNNER = Path("runner.py")
SUPERVISOR = Path("supervisor.py")
REGISTRY = Path("pipeline/job_registry.py")


def test_belief_triggers_dirty_instead_of_reconciling_inline():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "character_belief_reconciliation_dirty" in sql
    assert "mark_character_belief_dirty_descendants" in sql

    for function_name in (
        "refresh_belief_from_character_evidence",
        "refresh_belief_from_admission",
        "refresh_belief_from_acquisition_topology",
        "refresh_belief_from_ingest_supersession",
    ):
        block = sql.split(
            f"CREATE OR REPLACE FUNCTION aios.{function_name}", 1
        )[1].split("$$;", 1)[0]
        assert "mark_character_belief_dirty_descendants" in block
        assert "reconcile_belief_descendants" not in block


def test_dirty_expansion_preserves_descendant_continuity():
    sql = MIGRATION.read_text(encoding="utf-8")
    block = sql.split(
        "CREATE OR REPLACE FUNCTION aios.mark_character_belief_dirty_descendants", 1
    )[1].split("$$;", 1)[0]
    assert "WITH RECURSIVE descendants" in block
    assert "child.parent_instance_id=parent.instance_id" in block
    assert "ON CONFLICT (instance_id, atom_id) DO UPDATE" in block


def test_dirty_drain_is_bounded_and_version_safe():
    source = BELIEF.read_text(encoding="utf-8")
    block = source.split("async def reconcile_dirty_character_beliefs", 1)[1].split(
        "async def get_character_belief_states", 1
    )[0]
    assert "limit: int = 16" in block
    assert "reconcile_character_belief_atom(" in block
    assert "dirty_version=$3" in block


def test_reconciliation_stage_uses_reconciliation_pool():
    registry = REGISTRY.read_text(encoding="utf-8")
    supervisor = SUPERVISOR.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    assert '"reconcile_character_beliefs": JobSpec(ResourceClass.RECONCILIATION' in registry
    assert 'Stage("reconcile_character_beliefs", "reconcile_character_beliefs"' in supervisor
    assert '"reconcile_character_beliefs": handle_reconcile_character_beliefs' in runner
