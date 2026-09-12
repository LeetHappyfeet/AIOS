from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aios_app.epistemic.reconciliation_stage import (
    reconcile_claim_memory,
    reconcile_world_assertion,
)


class FakeDatabase:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, _sql, *_args):
        return self.row


def _claim_row(*, scope: str):
    return {
        "claim_id": uuid4(),
        "proposition_id": uuid4(),
        "atom_id": uuid4(),
        "epistemic_scope": scope,
        "world_id": uuid4(),
        "source_id": "test-source",
        "origin_character_id": "Renamon",
        "character_instance_id": uuid4(),
        "context_confidence": 0.91,
    }


def _assertion_row():
    return {
        "assertion_id": uuid4(),
        "world_id": uuid4(),
        "proposition_id": uuid4(),
        "atom_id": uuid4(),
        "epistemic_status": "corroborated",
        "source_kind": "observed",
        "confidence": 0.92,
    }


def test_character_path_never_updates_world_memory():
    row = _claim_row(scope="character")
    db = FakeDatabase(row)

    with (
        patch(
            "aios_app.epistemic.reconciliation_stage.reconcile_character_belief_atom",
            new=AsyncMock(),
        ) as reconcile_char,
        patch(
            "aios_app.epistemic.reconciliation_stage._reconcile_world_atom",
            new=AsyncMock(),
        ) as reconcile_world,
        patch(
            "aios_app.epistemic.reconciliation_stage._record_claim_receipt",
            new=AsyncMock(),
        ) as record_receipt,
    ):
        result = asyncio.run(reconcile_claim_memory(db, claim_id=row["claim_id"]))

    assert result.path == "char"
    reconcile_char.assert_awaited_once_with(
        db,
        instance_id=row["character_instance_id"],
        atom_id=row["atom_id"],
    )
    reconcile_world.assert_not_awaited()
    assert record_receipt.await_args.kwargs["path"] == "char"


def test_narrative_claim_with_world_id_does_not_become_world_memory():
    row = _claim_row(scope="narrative")
    row["source_id"] = None
    row["character_instance_id"] = None
    db = FakeDatabase(row)

    with (
        patch(
            "aios_app.epistemic.reconciliation_stage.reconcile_character_belief_atom",
            new=AsyncMock(),
        ) as reconcile_char,
        patch(
            "aios_app.epistemic.reconciliation_stage._reconcile_world_atom",
            new=AsyncMock(),
        ) as reconcile_world,
        patch(
            "aios_app.epistemic.reconciliation_stage._record_claim_receipt",
            new=AsyncMock(),
        ) as record_receipt,
    ):
        result = asyncio.run(reconcile_claim_memory(db, claim_id=row["claim_id"]))

    assert result.path == "evidence"
    assert result.outcome == "evidence_only"
    assert "narrative-evidence" in result.scope_key
    reconcile_char.assert_not_awaited()
    reconcile_world.assert_not_awaited()
    assert record_receipt.await_args.kwargs["path"] == "evidence"


def test_world_assertion_never_updates_character_belief():
    row = _assertion_row()
    db = FakeDatabase(row)

    with (
        patch(
            "aios_app.epistemic.reconciliation_stage.reconcile_character_belief_atom",
            new=AsyncMock(),
        ) as reconcile_char,
        patch(
            "aios_app.epistemic.reconciliation_stage._reconcile_world_atom",
            new=AsyncMock(return_value={"status": "materialized"}),
        ) as reconcile_world,
        patch(
            "aios_app.epistemic.reconciliation_stage._record_assertion_receipt",
            new=AsyncMock(),
        ) as record_receipt,
    ):
        result = asyncio.run(
            reconcile_world_assertion(db, assertion_id=row["assertion_id"])
        )

    assert result.path == "world"
    reconcile_world.assert_awaited_once_with(
        db,
        world_id=row["world_id"],
        atom_id=row["atom_id"],
    )
    reconcile_char.assert_not_awaited()
    record_receipt.assert_awaited_once()


def test_source_scope_stays_evidence_only():
    row = _claim_row(scope="source")
    row["character_instance_id"] = None
    db = FakeDatabase(row)

    with (
        patch(
            "aios_app.epistemic.reconciliation_stage.reconcile_character_belief_atom",
            new=AsyncMock(),
        ) as reconcile_char,
        patch(
            "aios_app.epistemic.reconciliation_stage._reconcile_world_atom",
            new=AsyncMock(),
        ) as reconcile_world,
        patch(
            "aios_app.epistemic.reconciliation_stage._record_claim_receipt",
            new=AsyncMock(),
        ) as record_receipt,
    ):
        result = asyncio.run(reconcile_claim_memory(db, claim_id=row["claim_id"]))

    assert result.path == "evidence"
    assert result.outcome == "evidence_only"
    reconcile_char.assert_not_awaited()
    reconcile_world.assert_not_awaited()
    assert record_receipt.await_args.kwargs["path"] == "evidence"
