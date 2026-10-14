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
    assert "source.value->>'raw_text'=cc.raw_text" in sql
    assert "source.value->>'frame_id'=f.frame_id::text" in sql
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
    assert "--supersede-id" in source
    assert "--actor" in source
    assert "semantic_hygiene_apply_enabled" in MIGRATION.read_text()


def test_targeted_vector_retirement_precedes_general_quarantine():
    eligibility = Path("semantic_index/eligibility.py").read_text()
    cli = Path("semantic_index/cli.py").read_text()
    block = eligibility.split("async def quarantine_adjudicated_vectors_once", 1)[1]
    assert "a.status IN ('applied','superseded')" in block
    assert "NOT aios.semantic_proposition_topology_eligible(a.proposition_id)" in block
    assert "pg_try_advisory_xact_lock($1)" in block
    assert "points_selector=selector" in block
    assert "wait=True" in block
    assert "NOT EXISTS" in block
    assert "character_belief_state" in block
    assert "DELETE FROM aios.proposition_evidence" not in block
    assert "DELETE FROM aios.claim_candidate" not in block
    assert cli.index('"hygiene-retirement"') < cli.index('"vector-propositions"')


def test_vector_retirement_noop_without_an_applied_candidate():
    from aios_app.semantic_index.config import SemanticIndexConfig
    from aios_app.semantic_index.eligibility import quarantine_adjudicated_vectors_once

    class NoCandidateDb:
        async def fetch(self, sql, *args):
            assert "semantic_hygiene_adjudication" in sql
            assert "NOT aios.semantic_proposition_topology_eligible" in sql
            return []

        def connection(self):
            raise AssertionError("No Qdrant mutation or SQL transaction for an empty batch")

    assert asyncio.run(
        quarantine_adjudicated_vectors_once(NoCandidateDb(), SemanticIndexConfig())
    ) == 0


def test_new_revision_requires_supervised_release_and_old_revision_stays_rejected():
    sql = Path("migrations/current/20261003_09_semantic_hygiene_revision_review.sql").read_text()
    assert "semantic_hygiene_occurrence_pending_review" in sql
    assert "hygiene_revision_requires_review" in sql
    assert "a.status IN ('applied','superseded')" in sql
    assert "supersede_semantic_hygiene_adjudication" in sql
    assert "si.status='valid'" in sql
    assert "recompute_semantic_evidence_admission" in sql
    assert "mark_character_belief_dirty_descendants" in sql
    assert "current_setting('aios.semantic_hygiene_apply_enabled',true)" in sql
