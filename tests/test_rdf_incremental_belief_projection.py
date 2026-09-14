import asyncio
from uuid import uuid4

from aios_app.rdf.epistemic_writer import (
    BELIEF_RDF_VERSION,
    _belief_iris,
    _belief_projection_hash,
    _belief_triples,
    project_character_belief_state,
)


def _belief_row(atom_id):
    return {
        "belief_atom_id": atom_id,
        "stance": "positive",
        "positive_support": 0.9,
        "negative_support": 0.1,
        "belief_confidence": 0.8,
        "preferred_proposition_id": uuid4(),
        "evidence_count": 3,
        "independent_evidence_count": 2,
        "resolved_through_node_id": uuid4(),
        "resolver_version": "character-belief-v1",
        "subject_norm": "alex",
        "predicate_norm": "knows",
        "object_norm": "door code",
    }


def test_belief_projection_hash_ignores_operational_timestamps():
    atom_id = uuid4()
    first = _belief_row(atom_id)
    second = dict(first)
    first["resolved_at"] = "2026-09-14T10:00:00Z"
    first["updated_at"] = "2026-09-14T10:00:00Z"
    second["resolved_at"] = "2026-09-14T10:01:00Z"
    second["updated_at"] = "2026-09-14T10:01:00Z"

    assert _belief_projection_hash(first) == _belief_projection_hash(second)


def test_belief_projection_hash_changes_with_semantic_state():
    atom_id = uuid4()
    first = _belief_row(atom_id)
    second = dict(first)
    second["stance"] = "negative"

    assert _belief_projection_hash(first) != _belief_projection_hash(second)


def test_belief_rdf_uses_stable_atom_scoped_resource_and_no_reconciliation_timestamp():
    instance_id = uuid4()
    atom_id = uuid4()
    row = _belief_row(atom_id)
    belief_iri, atom_iri = _belief_iris(
        character_id="Alex",
        instance_id=instance_id,
        atom_id=atom_id,
    )
    triples = "\n".join(
        _belief_triples(
            row,
            character_id="Alex",
            instance_id=instance_id,
            char_iri="urn:aios:char:Alex",
            instance_iri=f"urn:aios:character-instance:{instance_id}",
        )
    )

    assert belief_iri in triples
    assert atom_iri in triples
    assert BELIEF_RDF_VERSION in triples
    assert "resolvedAt" not in triples


class _NoDirtyDatabase:
    async def fetchrow(self, _sql, *_args):
        return {"character_id": "Alex"}

    async def fetch(self, sql, *_args):
        assert "rdf_character_belief_dirty" in sql
        return []


class _NoWriteFuseki:
    def update(self, *_args, **_kwargs):
        raise AssertionError("unchanged belief state must perform zero Fuseki writes")


def test_no_dirty_beliefs_perform_zero_fuseki_writes():
    result = asyncio.run(
        project_character_belief_state(
            _NoDirtyDatabase(),
            _NoWriteFuseki(),
            instance_id=uuid4(),
        )
    )
    assert result is True
