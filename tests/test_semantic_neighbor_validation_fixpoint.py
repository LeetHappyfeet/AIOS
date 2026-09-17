from types import SimpleNamespace
from uuid import uuid4

import pytest

import aios_app.semantic_index as semantic_index
from aios_app.epistemic.hypothesis_validation import apply_local_revalidation_invalidations
from aios_app.semantic_index import neighbor_classifier


class RecordingDB:
    def __init__(self, *, rows=None, inserted=None):
        self.rows = rows or []
        self.inserted = inserted
        self.executed = []
        self.fetchrow_calls = []

    async def fetch(self, query, *args):
        return self.rows

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.inserted

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"


@pytest.mark.asyncio
async def test_stale_pair_validation_preserves_classifier_receipt():
    db = RecordingDB()
    a = uuid4()
    b = uuid4()

    await apply_local_revalidation_invalidations(
        db,
        [{
            "decision_type": "proposition_relation",
            "decision_key": f"{a}:{b}",
            "subject_type": "proposition_pair",
            "subject_key": f"{a}:{b}",
        }],
    )

    assert db.executed == []


@pytest.mark.asyncio
async def test_neighbor_classifier_counts_only_successful_insert(monkeypatch):
    a = uuid4()
    b = uuid4()
    row = {
        "proposition_id": a,
        "neighbor_proposition_id": b,
        "similarity": 0.9,
        "a_topic_key": "topic",
        "a_subject_norm": "a",
        "a_predicate_norm": "knows",
        "a_object_norm": "x",
        "a_polarity": 1,
        "a_claim_kind": "STATE",
        "a_predicate_family": "relation",
        "a_world_id": None,
        "a_timeline_id": None,
        "a_epistemic_scope": "character",
        "a_character_id": "A",
        "a_character_instance_id": None,
        "a_viewpoint_id": None,
        "b_topic_key": "topic",
        "b_subject_norm": "b",
        "b_predicate_norm": "knows",
        "b_object_norm": "x",
        "b_polarity": 1,
        "b_claim_kind": "STATE",
        "b_predicate_family": "relation",
        "b_world_id": None,
        "b_timeline_id": None,
        "b_epistemic_scope": "character",
        "b_character_id": "B",
        "b_character_instance_id": None,
        "b_viewpoint_id": None,
        "conflict_type": None,
    }
    cfg = SimpleNamespace(
        embedding_version="test-embedding",
        batch_size=64,
    )

    monkeypatch.setattr(
        neighbor_classifier,
        "classify_neighbor_pair",
        lambda **kwargs: ("RELATED", 0.8, {"verifier_version": "test"}),
    )

    conflict_db = RecordingDB(rows=[row], inserted=None)
    written = await semantic_index._original_neighbor_classifier(conflict_db, cfg)
    assert written == 0

    inserted_db = RecordingDB(rows=[row], inserted={"inserted": 1})
    written = await semantic_index._original_neighbor_classifier(inserted_db, cfg)
    assert written == 1
    assert "RETURNING 1 AS inserted" in inserted_db.fetchrow_calls[0][0]
