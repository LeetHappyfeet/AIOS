-- Canonical reference data required by the AIOS baseline.
--
-- aios_baseline.sql is intentionally schema-only. Static policy/configuration
-- rows that define runtime behavior live here so a fresh database is both
-- structurally and behaviorally complete.
BEGIN;

INSERT INTO aios.reconciliation_family_policy
    (predicate_family, policy_name, policy_mode, accept_support, decision_margin, exclusive_slot, resolver_version)
VALUES
    ('UNKNOWN',       'generic',           'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('IDENTITY',      'identity',          'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('SOCIAL',        'relationship',      'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('MEMBERSHIP',    'membership',        'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('POSSESSION',    'possession',        'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('EPISTEMIC',     'epistemic',         'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('MEMORY',        'memory',            'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('CAUSAL',        'causal',            'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('COMMUNICATION', 'communication',     'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('ACTION',        'event',             'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('TEMPORAL',      'temporal',          'accumulate', 0.60, 0.15, false, 'semantic-policy-v1'),
    ('DESCRIPTIVE',   'descriptive_state', 'latest',     0.50, 0.05, false, 'semantic-policy-v1'),
    ('EMOTIONAL',     'emotional_state',   'latest',     0.50, 0.05, false, 'semantic-policy-v1'),
    ('GOAL',          'goal_lifecycle',    'latest',     0.50, 0.05, false, 'semantic-policy-v1'),
    ('SPATIAL',       'location_state',    'latest',     0.50, 0.05, true,  'semantic-policy-v1'),
    ('RULE',          'authority_rule',    'max',        0.70, 0.15, false, 'semantic-policy-v1')
ON CONFLICT (predicate_family) DO UPDATE
SET policy_name = EXCLUDED.policy_name,
    policy_mode = EXCLUDED.policy_mode,
    accept_support = EXCLUDED.accept_support,
    decision_margin = EXCLUDED.decision_margin,
    exclusive_slot = EXCLUDED.exclusive_slot,
    resolver_version = EXCLUDED.resolver_version,
    updated_at = now();

COMMIT;
