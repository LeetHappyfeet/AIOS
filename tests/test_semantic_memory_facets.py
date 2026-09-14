from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "current" / "20260914_semantic_memory_facets.sql"


def migration_text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_generic_descriptive_facets_are_not_exclusive():
    sql = migration_text()
    assert "'descriptive_state','exclusive',false" in sql
    assert "'descriptive_trait','exclusive',false" in sql
    assert "'descriptive_identity','exclusive',false" in sql


def test_narrow_slots_are_explicitly_exclusive():
    sql = migration_text()
    assert "'current_location','exclusive',true" in sql
    assert "'status','exclusive',true" in sql
    assert "'identity','exclusive',true" in sql


def test_facet_layer_preserves_members_instead_of_merging_atoms():
    sql = migration_text()
    assert "CREATE OR REPLACE VIEW aios.semantic_memory_facet_member" in sql
    assert "CREATE OR REPLACE VIEW aios.semantic_memory_facet" in sql
    assert "jsonb_agg(" in sql
    assert "array_agg(atom_id" in sql


def test_facets_are_built_from_reconciled_memory_surface():
    sql = migration_text()
    assert "FROM aios.semantic_memory_surface sms" in sql
    assert "FROM aios.claim_candidate" not in sql
