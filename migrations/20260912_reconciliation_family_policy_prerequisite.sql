-- Prerequisite for the reconciliation policy migrations.
--
-- This file intentionally sorts before
-- 20260912_reconciliation_policy_alignment.sql.  The alignment migration was
-- already applied on some installations and is therefore immutable; creating
-- the table here makes that historical migration safe for fresh installs while
-- preserving its recorded checksum on existing databases.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.reconciliation_family_policy (
    predicate_family text PRIMARY KEY,
    policy_name text NOT NULL,
    policy_mode text NOT NULL CHECK (policy_mode IN ('accumulate','latest','max')),
    accept_support double precision NOT NULL CHECK (accept_support BETWEEN 0 AND 1),
    decision_margin double precision NOT NULL CHECK (decision_margin BETWEEN 0 AND 1),
    exclusive_slot boolean NOT NULL DEFAULT false,
    resolver_version text NOT NULL DEFAULT 'semantic-policy-v1',
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMIT;
