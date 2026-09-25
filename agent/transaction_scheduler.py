from __future__ import annotations

from typing import Any
from uuid import UUID

from aios_app.db import Database
from .transactions import InternalCognitionTransactions


class TransactionScheduler:
    """Fill donated inference slots with highest-priority disposable HUD work."""

    def __init__(self, db: Database):
        self.db=db
        self.transactions=InternalCognitionTransactions(db)

    async def run_pending(self, limit:int=8) -> int:
        rows=await self.db.fetch(
            """SELECT transaction_id FROM aios.internal_cognition_transaction
               WHERE status='pending' AND expires_at>now()
               ORDER BY priority,created_at LIMIT $1""", max(1,min(int(limit),64))
        )
        completed=0
        for row in rows:
            try:
                await self.transactions.run(row["transaction_id"])
                completed += 1
            except Exception:
                # Provider busy/unavailable leaves an auditable failed receipt;
                # future admission creates a fresh transaction, never resurrects it.
                continue
        await self.db.execute(
            """UPDATE aios.internal_cognition_transaction
               SET status='stale',rejection_reason='expired before inference',updated_at=now()
               WHERE status='pending' AND expires_at<=now()"""
        )
        return completed
