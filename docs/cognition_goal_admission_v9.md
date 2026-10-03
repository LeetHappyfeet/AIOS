# Cognition V9: source-owned goal admission and ordered recovery

This is patch two of the source-DAG cognition repair, following
\`20261003_01_cognition_catchup_queue.sql\`. Production code and new regression
fixtures are character-agnostic. Existing historical interpreter receipts are
never silently relabeled.

## Contracts

1. Message Cognition now installs the epistemic-scope policy at its own module
   boundary, independent of import order. The persisted effective version is
   \`message-cognition-v9-candidate-admission+epistemic-scope-v1\`.
2. Explicit first-person commitments with a sufficiently specific action
   produce self-owned GOAL evidence; short/elliptical commitments are passed
   to bounded enrichment. An expressed refusal such as "I won't leave" is
   scene-position STATE evidence, not an automatic new task. Conditional
   intentions and contextual agreements require source-grounded adoption.
3. Each cognition commit records a bounded \`candidate_rejections\` list,
   \`candidate_outcome\`, audit version and completeness indicator. A processed
   zero-unit node remains a successful commit receipt and is distinguishable
   from a missing cognition commit.
4. Inference admission requires character-authored testimony. The model
   cannot assign a goal from somebody else's request; rejected model outputs
   are recorded in \`enrichment_rejections\`.
5. Historical catch-up only writes source-tied cognitive units. Completion
   first checks for still-missing current-branch nodes, runs at most four
   deferred model reviews per round, then reconciles eligible historical GOAL
   units. Unavailable inference returns \`source_inference_unavailable\` and
   prevents premature authoritative projection.
6. \`deferred_goal_reconciliation.py\` pins the current source head, considers
   current-branch evidence in event/ordinal order, and projects only the latest
   eligible GOAL for a topic. An existing newer or independent managed goal,
   terminal lifecycle, or immediate-only inferred action is preserved rather
   than retroactively resurrected. Reviews are recorded on the source unit.
   No historical source node may reset the live HUD cursor.

The participation V1/V2/V3 policies themselves are unchanged. Active-goal
participation context and HUD attention continue to use
\`aios.character_agent_goal\` through the existing goal service.

## Deployment

Pull both patches, apply the queue-index migration once if the active
migrator has not already applied it, and restart all API/runner workers so
each process loads the same effective interpreter version.

\`\`\`bash
cd ~/AIOS/aios_app/beta
git pull origin AIOS-development

psql "$AIOS_DB_DSN" -v ON_ERROR_STOP=1 \
  -f migrations/current/20261003_01_cognition_catchup_queue.sql

pytest -q \
  tests/test_cognition_admission_v9.py \
  tests/test_goal_admission_flow_v9.py \
  tests/test_deferred_goal_reconciliation.py \
  tests/test_cognition_catchup.py \
  tests/test_message_cognition.py \
  tests/test_epistemic_scope.py \
  tests/test_runtime_version_manifest.py
\`\`\`

The connection string \`$AIOS_DB_DSN\` should resolve to the active development
database. If it is unset, supply the actual DSN explicitly.

For an existing instance, perform bounded recovery without a new roleplay
turn. The command now includes both gap recovery and deferred reconciliation:

\`\`\`bash
python -m aios_app.epistemic.cognition_catchup \
  --instance-id INSTANCE_UUID_HERE --max-batches 8
\`\`\`

Review the JSON receipts. \`ready\` means the bounded scan found no remaining
eligible work, not that the language model's interpretations were proven
correct. \`source_inference_unavailable\`, \`source_inference_pending\`,
\`source_cognition_pending\` or \`goal_reconciliation_pending\` must not be
reported as successful lifecycle convergence.

## Audit SQL

\`\`\`sql
SELECT c.event_id,c.interpreter_version,
       c.summary->>'candidate_outcome' AS candidate_outcome,
       c.summary->'candidate_rejections' AS rejected_candidates,
       c.summary->>'historical_catchup' AS historical,
       c.summary->>'enrichment_deferred' AS enrichment_deferred,
       c.summary->>'goal_projection_deferred' AS goal_deferred
FROM aios.message_cognitive_commit c
WHERE c.instance_id = :instance_id
ORDER BY c.event_id;

SELECT c.event_id,u.claim_kind,u.polarity,u.text,
       u.meta->>'objective' AS objective,
       u.meta->>'historical_goal_reconciliation' AS history_decision
FROM aios.message_cognitive_commit c
JOIN aios.message_cognitive_unit u ON u.commit_id=c.commit_id
WHERE c.instance_id = :instance_id
ORDER BY c.event_id,u.ordinal;

SELECT goal_id,goal_text,status,priority,source_node_id,meta
FROM aios.character_agent_goal
WHERE instance_id = :instance_id
ORDER BY created_at,goal_id;
\`\`\`

In psql, replace \`:instance_id\` with a UUID literal or set a psql variable
first. Preserve source/commit IDs and the full rejection receipts when
comparing successive experiments.

Existing older-version commits remain historical. This patch recovers *missing*
source cognition; intentionally reinterpreting a committed older version
requires a separate opt-in migration or fresh test instance, with evidence
supersession rather than blind destructive replay.
