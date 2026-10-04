"""Integrity V4 persistence includes the actual source occurrence and revision."""
import asyncio
import hashlib
from uuid import uuid4

from aios_app.epistemic.semantic_integrity import validate_claim


class ReceiptDB:
    def __init__(self):
        self.claim = uuid4()
        self.section = uuid4()
        self.node = uuid4()
        self.writer_sql = ""
        self.writer_args = ()

    async def fetchrow(self, sql, *args):
        assert "es.sentence_text AS source_sentence" in sql
        assert "ds.section_id AS source_section_id" in sql
        return {
            "raw_text": "Alex saw Renamon.",
            "speaker_id": "Alex",
            "source_section": "Alex saw Renamon. A subsequent sentence.",
            "source_sentence": "Alex saw Renamon.",
            "source_section_id": self.section,
            "node_id": self.node,
        }

    async def fetch(self, sql, *args):
        return [{
            "frame_id": uuid4(),
            "resolved_subject": "Alex",
            "subject_text": "Alex",
            "predicate_canonical": "see",
            "predicate_surface": "saw",
            "resolved_object": "Renamon",
            "object_text": "Renamon",
            "polarity": 1,
            "modality": "asserted",
            "resolution_status": "resolved",
            "meta": {},
        }]

    async def execute(self, sql, *args):
        self.writer_sql, self.writer_args = sql, args


def test_v4_receipt_binds_section_sentence_and_source_dag_node():
    db = ReceiptDB()
    # Preserve one frame across the comparisons: changing the SOURCE only
    # must alter the revision key, not the semantic extraction fixture.
    frame = asyncio.run(db.fetch("", db.claim))
    async def fixed_fetch(sql, *args):
        return frame
    db.fetch = fixed_fetch
    asyncio.run(validate_claim(db, claim_id=db.claim))
    sql, args = db.writer_sql, db.writer_args
    assert "source_section_digest" in sql and "source_sentence_digest" in sql
    assert "source_section_id" in sql and "source_node_id" in sql
    assert args[7] == hashlib.sha256(
        b"Alex saw Renamon. A subsequent sentence."
    ).hexdigest()
    assert args[8] == db.section
    assert args[9] == hashlib.sha256(b"Alex saw Renamon.").hexdigest()
    assert args[10] == db.node
    original_revision = args[1]

    db.section = uuid4()
    db.node = uuid4()
    asyncio.run(validate_claim(db, claim_id=db.claim))
    assert db.writer_args[1] != original_revision
    assert db.writer_args[8] == db.section
    assert db.writer_args[10] == db.node
