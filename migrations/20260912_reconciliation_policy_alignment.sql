BEGIN;

-- This alignment migration sorts before the policy-engine migration. On a
-- database that has not installed the engine yet, there is nothing to align.
-- Use dynamic SQL so PostgreSQL does not resolve the relation while compiling
-- the DO block; a static UPDATE would still fail before the IF can protect it.
DO $$
BEGIN
    IF to_regclass('aios.reconciliation_family_policy') IS NOT NULL THEN
        EXECUTE $sql$
            UPDATE aios.reconciliation_family_policy
            SET accept_support=0.60,
                decision_margin=0.15,
                updated_at=now()
            WHERE policy_mode='accumulate'
        $sql$;
    END IF;
END;
$$;

COMMIT;
