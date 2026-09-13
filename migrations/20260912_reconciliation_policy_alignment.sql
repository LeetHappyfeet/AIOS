BEGIN;

-- This alignment migration sorts before the policy-engine migration. On a
-- database that has not installed the engine yet, there is nothing to align.
-- Guard the update so fresh installs and partially migrated installs are both
-- safe; the engine migration below seeds the aligned values itself.
DO $$
BEGIN
    IF to_regclass('aios.reconciliation_family_policy') IS NOT NULL THEN
        UPDATE aios.reconciliation_family_policy
        SET accept_support=0.60,
            decision_margin=0.15,
            updated_at=now()
        WHERE policy_mode='accumulate';
    END IF;
END;
$$;

COMMIT;
