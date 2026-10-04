# Integrated belief policy and V4 execution repair

## Root cause

The September 28 authority-lineage SQL was the live character belief
materializer but still selected thresholds from the legacy
aios.belief_reconciliation_policy table. The fresh reference migration
seeded only reconciliation_family_policy. Without a default row, SQL
comparisons returned NULL and every evidence-bearing stance fell through
to unresolved. Experiment 7 had 519 unresolved beliefs; 295 and 50 met
generic positive and negative numeric cutoffs respectively. Those counts
are diagnostic ONLY, not authority to promote malformed evidence.

The replacement materializer also stopped invoking the preexisting family
policy stage. Source eligibility was not uniformly enforced for topology.

## Migrations and semantics

20261003_10_integrated_belief_policy_and_integrity.sql:

- Seed legacy default from canonical UNKNOWN family policy; require all 16.
- Keep V3 authority-lineage support aggregation, under the same serialized
  lock; invoke the family policy afterward with the same cognitive ancestry,
  authority/admission rules, source-integrity restrictions.
- Restore latest/max/exclusive policies without restoring authority-blind
  evidence access. A missing policy now throws instead of returning
  apparently meaningful unresolved beliefs.
- Check current claim text and semantic frame snapshot for a current
  integrity receipt. New V4 receipts also carry source_section_digest.
  Historical V3 receipts without paragraph digest are tagged legacy-eligible
  only when their current claim and V2 frame snapshot still match. They are
  not silently certified as V4.
- Reuse source eligibility in admission, topology, vector eligibility
  (through topology predicates), and character belief materialization.
  Keep operator-gated hygiene exclusions; do not erase historical evidence.
- No automatic mass rewrite of existing character beliefs.

20261003_11_integrity_context_invalidation.sql:

- Recompute admission after changes to source paragraph or important
  semantic-frame fields; integrity receipt changes also trigger refresh.
- Existing dirty-set triggers then queue descendants for reconciliation.

20261003_12_occurrence_completion_invalidation.sql:

- Revisit earlier unresolved admission decisions if observation/frame bindings
  or standalone interpretation rows arrive AFTER acquisition. This closes the
  conservative source-gate ordering hole without guessing missing evidence.

The migrator applies all three files lexically after 08 and 09. db_check.py
checks the default and family policy rows, actual integrated SQL wrapper,
and three source-integrity gates during startup.

## Safe migration of Experiment 7 belief rows

Once the active experiment is captured/frozen, and the updated runner is
running, execute in psql:

    SELECT aios.assert_belief_policy_configuration();

    SELECT aios.queue_belief_policy_reconciliation(
      'b89ef0d9-d9da-4162-8f13-b5c285173a9b'::uuid, 32);

    SELECT count(*) AS pending
      FROM aios.character_belief_reconciliation_dirty
     WHERE instance_id =
       'b89ef0d9-d9da-4162-8f13-b5c285173a9b'::uuid;

The existing serialized worker must drain each bounded batch. Inspect
the resulting stances, effective source admission and hygiene audit,
then repeat until old resolver-version coordinates have been revisited.
NEVER update stance directly based on the 295/50 generic counts.

## Why V4 comparisons returned zero rows

Participation V4 is an additional SHADOW comparator, not live memory
admission. Participation V1 remains the shadow baseline; V2/V3/V4 are
embedded in its evaluation JSON. A separate participation worker must
consume the pending queue. The original launcher did not start it, so
enrollment and ingestion triggers could enqueue claims indefinitely
without generating evaluation receipts.

For a NEW controlled participation experiment, explicitly set:

    export AIOS_PARTICIPATION_SHADOW_ENABLED=1
    export AIOS_SEMANTIC_HYGIENE_APPLY_ENABLED=0
    python -m aios_app.launch

This adds an OPTIONAL Participation Shadow service with a distinct
AIOS_READY marker; it does not alter memory admission. Inspection now
exposes evaluation_coverage and flags zero evaluations or expired
windows. Previous paired receipts can be compared from frozen snapshots,
but an expired experiment with no evaluations cannot reconstruct
historical live-state comparisons.

Before discussing foreground-share differences, inspect:

    SELECT experiment_id, instance_id, enqueued_count, status, until_at
      FROM aios.character_participation_experiment
     WHERE experiment_id =
       '4584cfe6-ecb7-4c5f-8091-87457b0d6c73'::uuid;

    SELECT status,count(*),max(last_error) AS last_error
      FROM aios.character_participation_pending
     WHERE experiment_id =
       '4584cfe6-ecb7-4c5f-8091-87457b0d6c73'::uuid
     GROUP BY status;

    SELECT count(*) AS evaluated,
           count(*) FILTER (WHERE signals ? 'comparison_v3') AS v3_count,
           count(*) FILTER (WHERE signals ? 'comparison_v4') AS v4_count,
           count(*) FILTER (
             WHERE signals->'comparison_v4'->>'evaluation_mode'='paired_live'
           ) AS live_v4_count
      FROM aios.character_participation_evaluation
     WHERE experiment_id =
       '4584cfe6-ecb7-4c5f-8091-87457b0d6c73'::uuid;

Do not promote participation V4 as live policy without reviewing
paired disagreements against independently annotated source claims.

## CI

The semantic-topology workflow tests the canonical fresh baseline and
active policy/integrity migrations, including missing-default failure,
V3+family composition, source paragraph digest and integrity invalidation.
Old CI jobs pointing to removed prototype migrations have been replaced
with fresh-baseline smoke tests; the causal baseline smoke is separate.
The optional shadow worker has an explicit zero-evaluation/expiry test.
