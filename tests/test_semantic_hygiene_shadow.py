"""Source-grounded semantic hygiene stays an auditable, read-only shadow pass."""
from pathlib import Path
from uuid import UUID

import pytest

from aios_app.epistemic.semantic_hygiene import (
    POLICY_VERSION, _rdf_snapshot, classify_candidate, run_shadow_batch,
)

ATOM = UUID("16432edb-9565-468d-ba9d-75da96c572f0")


def _atom(subject="mia and", predicate="keep", obj=None):
    return {"atom_id": ATOM, "subject_norm": subject,
            "predicate_norm": predicate, "object_norm": obj}


def _lineage(*, relation=None, standalone=True, source="Mia and Renamon packed up."):
    return [{"frame_meta": {"clause_relation": relation,
                            "standalone_semantic": standalone},
             "raw_text": source}]


def test_malformed_coordination_is_a_repair_proposal_not_a_deletion():
    disposition, reasons = classify_candidate(
        _atom(), _lineage(), {"belief_states": 2}, [
            {"graph": "urn:test", "subject": "mia and",
             "predicate": "keep", "object": None},
        ],
    )
    assert disposition == "repair_candidate"
    assert "suspect_subject_boundary" in reasons
    assert "missing_expected_argument" in reasons


def test_dependent_clause_is_demoted_not_assumed_false():
    disposition, reasons = classify_candidate(
        _atom("a script", "have", "an apartment"),
        _lineage(relation="relcl", standalone=False),
        {"belief_states": 0}, [],
    )
    assert disposition == "demote_candidate"
    assert "dependent_frame_requires_parent" in reasons


def test_resultative_go_is_detected_from_original_source():
    disposition, reasons = classify_candidate(
        _atom("her tail", "go"),
        _lineage(source="Her tail has gone still."),
        {"belief_states": 0}, [],
    )
    assert disposition == "repair_candidate"
    assert "resultative_state_lost" in reasons


def test_rdf_parity_anomaly_requires_review_not_prune():
    disposition, reasons = classify_candidate(
        _atom("renamon", "walk", "outside"),
        _lineage(), {"belief_states": 1},
        [{"graph": "urn:test", "subject": "renamon",
          "predicate": "run", "object": "outside"}],
    )
    assert disposition == "needs_review"
    assert "rdf_sql_representation_mismatch" in reasons


def test_absent_rdf_cannot_prove_proposition_invalid():
    disposition, reasons = classify_candidate(
        _atom("renamon", "walk", "outside"),
        _lineage(), {"belief_states": 1}, [],
    )
    assert disposition == "needs_review"
    assert "rdf_not_observed_for_belief" in reasons


class FakeFuseki:
    def __init__(self, fail=False):
        self.fail = fail
        self.queries = []
        self.updates = []

    def query(self, dataset, sparql):
        self.queries.append((dataset, sparql))
        if self.fail:
            raise RuntimeError("Fuseki unavailable")
        return {"results": {"bindings": [{
            "atom": {"value": "urn:aios:semantic-atom:" + str(ATOM)},
            "graph": {"value": "urn:aios:char:Renamon:epistemic"},
            "subject": {"value": "mia and"},
            "predicate": {"value": "keep"},
        }]}}

    def update(self, *args):
        self.updates.append(args)
        raise AssertionError("shadow must never mutate RDF")


class FakeDb:
    def __init__(self):
        self.audit_inserts = []
        self.cursor_updates = []

    async def fetchrow(self, sql, *args):
        if "FROM aios.semantic_hygiene_shadow_cursor" in sql:
            return {"last_atom_id": None, "completed_at": None}
        if "AS belief_states" in sql:
            return {"propositions": 1, "belief_states": 8,
                    "affected_instances": 8, "evidence_records": 1,
                    "topology_nodes": 2, "topology_anchors": 0}
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "FROM coverage c" in sql:
            return [_atom()]
        if "FROM aios.proposition p" in sql:
            return [{
                "proposition_id": UUID(int=2), "canonical_text": "mia and | keep | _",
                "evidence_id": UUID(int=3), "evidence_role": "support",
                "observation_id": UUID(int=4), "claim_id": UUID(int=5),
                "raw_text": "You and Mia keep circling back.",
                "revision_key": "revision-1", "validator_version": "semantic-integrity-v3-fidelity",
                "integrity_status": "valid", "frame_id": UUID(int=6),
                "is_primary": True, "semantic_role": "clause",
                "frame_index": 0, "subject_text": "Mia and",
                "resolved_subject": "Mia and", "predicate_surface": "keep",
                "predicate_canonical": "keep", "object_text": None,
                "resolved_object": None, "modality": "asserted", "frame_meta": {},
            }]
        raise AssertionError(sql)

    async def execute_returning_row(self, sql, *args):
        assert "INSERT INTO aios.semantic_hygiene_shadow_audit" in sql
        self.audit_inserts.append((sql, args))
        return {"audit_id": UUID(int=7)}

    async def execute(self, sql, *args):
        assert "UPDATE aios.semantic_hygiene_shadow_cursor" in sql
        self.cursor_updates.append((sql, args))


def test_single_bounded_rdf_snapshot():
    fuseki = FakeFuseki()
    result = _rdf_snapshot(fuseki, [_atom()])
    assert fuseki.queries[0][0] == "char"
    assert "VALUES ?atom" in fuseki.queries[0][1]
    assert result[str(ATOM)][0]["predicate"] == "keep"
    assert not fuseki.updates


@pytest.mark.asyncio
async def test_fuseki_error_never_advances_cursor_or_writes_partial_audit():
    db, fuseki = FakeDb(), FakeFuseki(fail=True)
    with pytest.raises(RuntimeError, match="Fuseki unavailable"):
        await run_shadow_batch(db, fuseki)
    assert db.audit_inserts == []
    assert db.cursor_updates == []


@pytest.mark.asyncio
async def test_shadow_batch_only_writes_audit_and_cursor():
    db, fuseki = FakeDb(), FakeFuseki()
    outcome = await run_shadow_batch(db, fuseki, population="v3_only", limit=16)
    assert outcome["scanned"] == 1
    assert outcome["new_audits"] == 1
    assert len(db.audit_inserts) == len(db.cursor_updates) == 1
    assert db.audit_inserts[0][1][2] == POLICY_VERSION
    assert db.audit_inserts[0][1][4] == "repair_candidate"
    assert not fuseki.updates


def test_job_is_opt_in_background_and_baseline_remains_immutable():
    registry = Path("pipeline/job_registry.py").read_text()
    runner = Path("runner_v2.py").read_text()
    assert '"semantic_hygiene_shadow": JobSpec(ResourceClass.RDF' in registry
    assert '"compact_character_world_epistemic", "semantic_hygiene_shadow"' in registry
    assert 'AIOS_SEMANTIC_HYGIENE_SHADOW_ENABLED' in runner
    assert 'priority=290' in runner
    source = Path("epistemic/semantic_hygiene.py").read_text()
    assert "fuseki.update(" not in source
    assert "DELETE FROM aios.proposition_evidence" not in source
    assert "DELETE FROM aios.semantic_topology_node" not in source
    assert "DELETE FROM aios.semantic_atom" not in source
