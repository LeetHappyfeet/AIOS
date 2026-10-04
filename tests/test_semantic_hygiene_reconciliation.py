"""Semantic Hygiene Reconciliation V1 safety and Mia regression contracts.

The Postgres smoke test exercises actual source/admission/descendant behavior.
These tests additionally fail on accidental reintroduction of raw destructive
operations, revision-independent atom suppression or an unguarded CLI.
"""
import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from aios_app.epistemic import hygiene_reconciliation as repair

MIGRATION = Path("migrations/current/20261003_08_semantic_hygiene_reconciliation.sql")
MIA_CLAIM = UUID("211137e7-673e-46d2-895b-0b31007f3ae1")
MIA_FRAME = UUID("2456f078-85a2-446d-b35f-f8bb945cb2e6")
MIA_PROPOSITION = UUID("c7f7a9bd-e7f1-4374-93d6-0c34ace5d6a2")
MIA_ATOM = UUID("16432edb-9565-468d-ba9d-75da96c572f0")


def test_mia_source_is_exactly_scoped_by_claim_frame_proposition_and_revision():
    sql = MIGRATION.read_text()
    assert "p_claim uuid, p_frame uuid, p_proposition uuid" in sql
    assert "source->>'raw_text'=cc.raw_text" in sql
    assert "source->>'frame_id'=f.frame_id::text" in sql
    assert "a.source_revision_key=si.revision_key" in sql
    assert "a.validator_version=si.validator_version" in sql
    assert "a.source_text=cc.raw_text" in sql
    assert "a.status='applied'" in sql
    assert "status IN ('proposed','applied','superseded')" in sql


def test_admission_guard_prevents_recompute_reactivation():
    sql = MIGRATION.read_text()
    assert "CREATE TRIGGER trg_zz_semantic_hygiene_admission" in sql
    assert "BEFORE INSERT OR UPDATE ON aios.semantic_evidence_admission" in sql
    assert "NEW.status := 'suppressed'" in sql
    assert "NEW.confidence := 0" in sql
    assert "semantic_hygiene_acquisition_suppressed(NEW.acquisition_id)" in sql


def test_repair_reuses_dirty_descendants_and_rdf_delta_outbox():
    sql = MIGRATION.read_text()
    assert "mark_character_belief_dirty_descendants(r.instance_id,v_atom)" in sql
    assert "character_belief_reconciliation_dirty" in sql
    assert "dirty_version=dirty_version+1" in sql
    assert "trg_hygiene_topology_mutation_dirty" in sql
    assert "semantic_occurrence_topology_eligible(o.claim_id,op.proposition_id)" in sql
    for unsafe in (
        "DELETE FROM aios.claim_candidate",
        "DELETE FROM aios.observation",
        "DELETE FROM aios.proposition_evidence",
        "DELETE FROM aios.knowledge_acquisition_event",
        "DELETE FROM aios.semantic_atom",
        "DELETE WHERE {",
    ):
        assert unsafe not in sql


def test_both_topology_derivers_gate_the_exact_observation():
    for module in ("epistemic/topology.py", "epistemic/topology_claims.py"):
        source = Path(module).read_text()
        assert "semantic_claim_topology_admitted(ccr.claim_id)" in source
        assert "semantic_occurrence_topology_eligible(o.claim_id,o.proposition_id)" in source


class DisabledDb:
    def connection(self):
        raise AssertionError("A disabled repair must never connect to the write path")


def test_operator_apply_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AIOS_SEMANTIC_HYGIENE_APPLY_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        asyncio.run(
            repair.apply(
                DisabledDb(),
                adjudication_id=UUID(int=1),
                actor="operator",
            )
        )


def test_apply_is_explicitly_transaction_local():
    source = Path("epistemic/hygiene_reconciliation.py").read_text()
    assert "async with db.connection() as con:" in source
    assert "async with con.transaction():" in source
    assert "set_config('aios.semantic_hygiene_apply_enabled','on',true)" in source
    assert "--apply-id" in source
    assert "--actor" in source
    assert "semantic_hygiene_apply_enabled" in MIGRATION.read_text()
