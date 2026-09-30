"""Exercise the migration's actual SELECT bodies against occurrence fixtures."""
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aios_app.semantic_index import eligibility


@pytest.fixture
def evidence():
    db = sqlite3.connect(':memory:')
    db.execute("ATTACH DATABASE ':memory:' AS aios")
    for table, columns in {
        'observation': 'observation_id text, claim_id text, proposition_id text',
        'observation_proposition': 'observation_id text, proposition_id text, frame_id text',
        'claim_semantic_frame': 'frame_id text, claim_id text, resolution_status text',
        'semantic_interpretation': 'frame_id text, claim_id text, standalone_semantic boolean',
        'semantic_exact_admission': 'claim_id text, decision text',
        'semantic_neighbor_admission': 'claim_id text, decision text',
    }.items():
        db.execute(f'CREATE TABLE aios.{table} ({columns})')
    migration = (Path(__file__).parents[1] / 'migrations/current/20260930_01_semantic_topology_eligibility.sql').read_text()
    bodies = re.findall(r'FUNCTION aios\.(\w+)\((.*?)\)\s*RETURNS boolean LANGUAGE sql STABLE AS \$\$(.*?)\$\$', migration, re.S)
    functions = {}
    for name, params, body in bodies:
        names = [x.strip().split()[0] for x in params.split(',')]
        for param in names:
            body = re.sub(r'\b' + param + r'\b', ':' + param, body)
        body = re.sub(r'aios\.(semantic_\w+)\(', r'\1(', body)
        def run(*values, body=body, names=names):
            return bool(db.execute(body, dict(zip(names, values))).fetchone()[0])
        functions[name] = run
        db.create_function(name, len(names), run)
    def add(claim, prop, resolved, standalone, interpretation=True):
        frame = claim + '-frame'
        db.execute('INSERT INTO aios.observation VALUES (?,?,?)', (claim, claim, prop))
        db.execute('INSERT INTO aios.observation_proposition VALUES (?,?,?)', (claim, prop, frame))
        db.execute('INSERT INTO aios.claim_semantic_frame VALUES (?,?,?)', (frame, claim, resolved))
        if interpretation:
            db.execute('INSERT INTO aios.semantic_interpretation VALUES (?,?,?)', (frame, claim, standalone))
    return db, functions, add


def test_partial_fragment_preserved_but_ineligible(evidence):
    db, fn, add = evidence
    add('checking', 'check', 'partial', False)
    assert not fn['semantic_proposition_topology_eligible']('check')
    assert not fn['semantic_claim_topology_eligible']('checking')
    assert db.execute('SELECT count(*) FROM aios.observation').fetchone()[0] == 1


def test_resolved_support_does_not_certify_partial_occurrence(evidence):
    _, fn, add = evidence
    add('partial', 'check', 'partial', False)
    add('resolved', 'check', 'resolved', True)
    assert fn['semantic_proposition_topology_eligible']('check')
    assert fn['semantic_claim_topology_eligible']('resolved')
    assert not fn['semantic_occurrence_topology_eligible']('partial', 'check')


def test_missing_interpretation_fails_closed_and_repair_reopens(evidence):
    db, fn, add = evidence
    add('legacy', 'sleep', 'resolved', True, interpretation=False)
    assert not fn['semantic_proposition_topology_eligible']('sleep')
    db.execute("INSERT INTO aios.semantic_interpretation VALUES ('legacy-frame','legacy',true)")
    assert fn['semantic_proposition_topology_eligible']('sleep')
    db.execute("UPDATE aios.semantic_interpretation SET standalone_semantic=false")
    assert not fn['semantic_proposition_topology_eligible']('sleep')


def test_unrelated_frame_cannot_grant_authority(evidence):
    db, fn, add = evidence
    add('fragment', 'check', 'partial', False)
    add('complete', 'walk', 'resolved', True)
    assert not fn['semantic_occurrence_topology_eligible']('fragment', 'walk')
    assert not fn['semantic_proposition_topology_eligible']('check')


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_quarantine_deletes_both_collections_before_receipts(monkeypatch, fails):
    calls = []
    prop = uuid4()
    class DB:
        async def fetch(self, sql, *args):
            return [{'proposition_id': prop}]
        async def execute(self, sql, *args):
            calls.append(('sql', sql))
    class Client:
        def delete(self, **kwargs):
            selector = kwargs['points_selector']
            match = selector.filter.must[0].match
            calls.append(('vector', kwargs['collection_name'], list(match.any)))
            if fails:
                raise RuntimeError('Qdrant unavailable')
    monkeypatch.setattr(eligibility, '_get_store', lambda *args: SimpleNamespace(client=Client()))
    cfg = SimpleNamespace(proposition_collection='props', epistemic_collection='owners', batch_size=10)
    if fails:
        with pytest.raises(RuntimeError):
            await eligibility.quarantine_ineligible_vectors_once(DB(), cfg)
        assert not any('DELETE FROM aios.semantic_vector_index_state' in sql for call in calls if call[0] == 'sql' for sql in [call[1]])
    else:
        assert await eligibility.quarantine_ineligible_vectors_once(DB(), cfg) == 1
        vector_indexes = [i for i, call in enumerate(calls) if call[0] == 'vector']
        receipt_index = next(i for i, call in enumerate(calls) if call[0] == 'sql' and 'DELETE FROM aios.semantic_vector_index_state' in call[1])
        assert len(vector_indexes) == 2 and max(vector_indexes) < receipt_index
        assert {calls[i][1] for i in vector_indexes} == {'props', 'owners'}
        assert all(calls[i][2] == [str(prop)] for i in vector_indexes)
    assert not any(re.search(r'DELETE FROM aios\.(observation|proposition|claim_candidate)\b', sql) for call in calls if call[0] == 'sql' for sql in [call[1]])


def test_timeout_preserves_eligible_evidence_but_defers_topology(evidence):
    db, fn, add = evidence
    add('resolved', 'check', 'resolved', True)
    db.execute("INSERT INTO aios.semantic_exact_admission VALUES ('resolved','novel_exact')")
    db.execute("INSERT INTO aios.semantic_neighbor_admission VALUES ('resolved','bypass_timeout')")
    assert fn['semantic_proposition_topology_eligible']('check')
    assert not fn['semantic_proposition_topology_admitted']('check')
    db.execute("UPDATE aios.semantic_neighbor_admission SET decision='novel'")
    assert fn['semantic_proposition_topology_admitted']('check')
