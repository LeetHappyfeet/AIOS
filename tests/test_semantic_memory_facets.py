from pathlib import Path

from aios_app.epistemic.facets import collapse_facet_members


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


def test_multi_value_descriptive_state_keeps_all_members_compatible():
    collapsed = collapse_facet_members(
        [
            {"text": "mia | be | vulnerable", "confidence": 0.68, "stance": "positive", "polarity": 1},
            {"text": "mia | be | delicate", "confidence": 0.61, "stance": "positive", "polarity": 1},
            {"text": "mia | be | new", "confidence": 0.61, "stance": "positive", "polarity": 1},
        ],
        facet="STATE",
        facet_slot="descriptive_state",
        exclusive=False,
    )
    assert collapsed["member_count"] == 3
    assert collapsed["contested"] is False
    assert [m["text"] for m in collapsed["members"]] == [
        "mia | be | vulnerable",
        "mia | be | delicate",
        "mia | be | new",
    ]


def test_exclusive_slot_surfaces_competing_values_as_contested():
    collapsed = collapse_facet_members(
        [
            {"text": "mia | status | alive", "confidence": 0.8, "stance": "positive", "polarity": 1},
            {"text": "mia | status | dead", "confidence": 0.7, "stance": "positive", "polarity": 1},
        ],
        facet="STATE",
        facet_slot="status",
        exclusive=True,
    )
    assert collapsed["member_count"] == 2
    assert collapsed["contested"] is True
