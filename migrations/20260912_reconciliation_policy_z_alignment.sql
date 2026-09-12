BEGIN;

-- Runs after 20260912_reconciliation_policy_engine.sql. Keep SQL policy rows
-- aligned with epistemic/reconciliation_policy.py and with the legacy generic
-- accumulator used as the accumulated-family implementation.
UPDATE aios.reconciliation_family_policy
SET accept_support=0.60,
    decision_margin=0.15,
    updated_at=now()
WHERE policy_mode='accumulate';

COMMIT;
