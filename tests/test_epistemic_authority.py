from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_authority_migration_has_immutable_origin_and_lineage():
    sql = (ROOT / "migrations/current/20260927_epistemic_authority_membrane.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS aios.epistemic_authority_admission" in sql
    assert "generated_cognition_has_no_historical_authority" in sql
    assert "Origin and lineage deliberately never change" in sql
    update = sql.split("ON CONFLICT (acquisition_id) DO UPDATE SET", 1)[1]
    assert "origin_kind=EXCLUDED.origin_kind" not in update
    assert "lineage_key=EXCLUDED.lineage_key" not in update


def test_generated_cognition_is_subjective_only_by_default():
    sql = (ROOT / "migrations/current/20260927_epistemic_authority_membrane.sql").read_text()
    assert "v_origin IN ('model_generation','model_inference','character_generation')" in sql
    assert "v_uses:=ARRAY['belief','reflection']" in sql
    assert "action_precondition" in sql


def test_record_acquisition_accepts_explicit_provenance_contract():
    source = (ROOT / "epistemic/knowledge.py").read_text()
    assert "origin_kind: Optional[str] = None" in source
    assert "epistemic_mode: Optional[str] = None" in source
    assert "origin_lineage_id: Optional[str] = None" in source
    assert 'acquisition_meta["origin_kind"] = origin_kind' in source
