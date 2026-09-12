from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from aios_app.epistemic.retention import (
    RetentionState,
    get_retention_decision,
    set_retention_state,
)


class FakeDatabase:
    def __init__(self, *, retention_row=None, changed=True):
        self.retention_row = retention_row
        self.changed = changed
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "set_semantic_retention_state" in sql:
            return {"changed": self.changed}
        return self.retention_row


def test_missing_retention_row_is_implicitly_active():
    db = FakeDatabase(retention_row=None)
    artifact_id = uuid4()
    result = asyncio.run(
        get_retention_decision(
            db,
            artifact_type="knowledge_acquisition_event",
            artifact_id=artifact_id,
        )
    )
    assert result.state is RetentionState.ACTIVE
    assert result.reason_code == "implicit_active"
    assert result.protected is False


def test_retention_transition_serializes_metadata_and_never_deletes():
    db = FakeDatabase(changed=True)
    artifact_id = uuid4()
    changed = asyncio.run(
        set_retention_state(
            db,
            artifact_type="knowledge_acquisition_event",
            artifact_id=artifact_id,
            state=RetentionState.QUARANTINED,
            reason_code="duplicate_same_event_proposition",
            redundancy_score=1.0,
            recoverability=1.0,
            meta={"representative": "original"},
        )
    )
    assert changed is True
    sql, args = db.calls[-1]
    assert "set_semantic_retention_state" in sql
    assert "DELETE" not in sql.upper()
    assert args[2] == "QUARANTINED"
    assert json.loads(args[-1]) == {"representative": "original"}


def test_retention_state_round_trips_quarantine():
    db = FakeDatabase(
        retention_row={
            "state": "QUARANTINED",
            "reason_code": "extraction_debris:missing_semantic_predicate",
            "protected": False,
            "meta": '{"recoverable": true}',
        }
    )
    result = asyncio.run(
        get_retention_decision(
            db,
            artifact_type="claim_candidate",
            artifact_id=uuid4(),
        )
    )
    assert result.state is RetentionState.QUARANTINED
    assert result.detail["recoverable"] is True
