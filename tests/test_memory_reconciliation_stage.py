from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aios_app.epistemic.reconciliation_stage import reconcile_claim_memory


class FakeDatabase:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, _sql, *_args):
        return self.row


def _base_row(*, scope: str):
    claim_id = uuid4()
    proposition_id = uuid4()
    atom_id = uuid4()
    world_id = uuid4()
    instance_id = uuid4()
    return {
        "claim_id": claim_id,
        "proposition_id": proposition_id,
        "atom_id": atom_id,
        "epistemic_scope": scope,
        "world_id": world_id,
        "origin_character_id": "Renamon",
        "character_instance_id": instance_id,
        "context_confidence": 0.91,
    }


def test_character_path_never_updates_world_memory():
    row = _base_row(scope="character")
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
            "aios_app.epistemic.reconciliation_stage._record_receipt",
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


def test_world_path_never_updates_character_belief():
    row = _base_row(scope="world")
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
            "aios_app.epistemic.reconciliation_stage._record_receipt",
            new=AsyncMock(),
        ) as record_receipt,
    ):
        result = asyncio.run(reconcile_claim_memory(db, claim_id=row["claim_id"]))

    assert result.path == "world"
    reconcile_world.assert_awaited_once_with(
        db,
        world_id=row["world_id"],
        atom_id=row["atom_id"],
    )
    reconcile_char.assert_not_awaited()
    assert record_receipt.await_args.kwargs["path"] == "world"


def test_unresolved_scope_stays_evidence_only():
    row = _base_row(scope="source")
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
            "aios_app.epistemic.reconciliation_stage._record_receipt",
            new=AsyncMock(),
        ) as record_receipt,
    ):
        result = asyncio.run(reconcile_claim_memory(db, claim_id=row["claim_id"]))

    assert result.path == "evidence"
    assert result.outcome == "evidence_only"
    reconcile_char.assert_not_awaited()
    reconcile_world.assert_not_awaited()
    assert record_receipt.await_args.kwargs["path"] == "evidence"
