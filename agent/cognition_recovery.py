from __future__ import annotations

import logging

from aios_app.db import Database

logger = logging.getLogger(__name__)


class CognitionRecovery:
    """Reconcile domain receipts as well as disposable pipeline leases."""

    def __init__(self, db: Database):
        self.db = db

    async def recover(self, limit: int = 100) -> None:
        from .transactions import InternalCognitionTransactions
        from .cognitive_operations import CognitiveOperationEngine
        from .episodes import CognitiveEpisodeStore
        transactions=InternalCognitionTransactions(self.db)
        engine=CognitiveOperationEngine(self.db)
        expired=await self.db.fetch(
            """SELECT transaction_id FROM aios.internal_cognition_transaction
               WHERE status IN ('pending','running','proposed') AND expires_at<=now()
               ORDER BY expires_at LIMIT $1""",limit)
        for row in expired:
            try:
                await transactions._reject(row["transaction_id"],"stale","transaction deadline expired")
            except Exception:
                logger.exception("Expired transaction recovery failed transaction=%s",row["transaction_id"])
        terminal=await self.db.fetch(
            """SELECT transaction_id FROM aios.internal_cognition_transaction
               WHERE status IN ('consumed','failed','stale','invalid','superseded')
                 AND reconciled_at IS NULL ORDER BY updated_at LIMIT $1""",limit)
        for row in terminal:
            try:
                await transactions.reconcile_terminal(row["transaction_id"])
            except Exception:
                logger.exception("Transaction reconciliation failed transaction=%s",row["transaction_id"])
        abandoned=await self.db.fetch(
            """SELECT o.* FROM aios.character_cognitive_operation o
               LEFT JOIN aios.internal_cognition_transaction t ON t.transaction_id=o.transaction_id
               WHERE (o.status='running' AND o.updated_at<now()-interval '10 minutes'
                      AND NOT EXISTS (
                          SELECT 1 FROM aios.pipeline_job j
                          WHERE j.payload->>'operation_id'=o.operation_id::text AND j.status='running'
                            AND j.lease_expires_at>now()))
                  OR (o.status='waiting_inference' AND o.transaction_id IS NOT NULL
                      AND t.status IN ('failed','stale','invalid','superseded'))
                  OR (o.status='waiting_inference' AND o.transaction_id IS NULL
                      AND o.updated_at<now()-interval '10 minutes'
                      AND NOT EXISTS (
                          SELECT 1 FROM aios.pipeline_job j
                          WHERE j.payload->>'operation_id'=o.operation_id::text AND j.status IN ('queued','running')))
               ORDER BY o.updated_at LIMIT $1""",limit)
        for row in abandoned:
            try:
                await engine._stale(dict(row),"abandoned cognition or terminal decision transaction")
            except Exception:
                logger.exception("Operation recovery failed operation=%s",row["operation_id"])
        terminal_ops=await self.db.fetch(
            """SELECT operation_id FROM aios.character_cognitive_operation
               WHERE status IN ('succeeded','failed','stale') AND reconciled_at IS NULL
               ORDER BY updated_at LIMIT $1""",limit)
        for row in terminal_ops:
            try:
                await engine.reconcile_terminal(row["operation_id"])
            except Exception:
                logger.exception("Operation reconciliation failed operation=%s",row["operation_id"])
        # Old pre-atomic admissions may have committed queued operations
        # without their pipeline jobs. Repair their outbox, do not duplicate work.
        queued=await self.db.fetch(
            """SELECT operation_id,instance_id FROM aios.character_cognitive_operation o
               WHERE status='queued' AND NOT EXISTS (
                   SELECT 1 FROM aios.pipeline_job j
                   WHERE j.job_type='cognitive_operation' AND j.status IN ('queued','running')
                     AND j.payload->>'operation_id'=o.operation_id::text)
               ORDER BY created_at LIMIT $1""",limit)
        from aios_app.pipeline.jobs import enqueue_job
        for row in queued:
            try:
                async with self.db.atomic():
                    await self.db.fetchval("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                                           f"cognitive-recovery:{row['operation_id']}")
                    exists=await self.db.fetchval(
                        """SELECT 1 FROM aios.pipeline_job WHERE job_type='cognitive_operation'
                           AND status IN ('queued','running') AND payload->>'operation_id'=$1""",
                        str(row["operation_id"]))
                    if not exists:
                        await enqueue_job(self.db,job_type="cognitive_operation",
                            payload={"operation_id":str(row["operation_id"]),"instance_id":str(row["instance_id"])})
            except Exception:
                logger.exception("Cognitive outbox repair failed operation=%s",row["operation_id"])
        await CognitiveEpisodeStore(self.db).reconcile_pending(limit)
