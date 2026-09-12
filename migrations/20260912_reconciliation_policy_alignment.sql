BEGIN;

UPDATE aios.reconciliation_family_policy
SET accept_support=0.60,
    decision_margin=0.15,
    updated_at=now()
WHERE policy_mode='accumulate';

COMMIT;
