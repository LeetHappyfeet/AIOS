# Experiment 5 — source-DAG cognition catch-up and V9 goal admission

This patch responds to experiment
`9d8bdc27-2e2e-44a2-a4f3-ca666830c592`,
a newly created character instance.
Events **9–14** have ordinary extracted claims but no
`message_cognitive_commit`; events 15, 30 and 31 do have V4-stamped commits.
The V4 stamp was caused by `install_message_cognition_scope_guard` resetting
the effective version on top of the V8 implementation.

## Changes

- The effective interpreter version is now
  `message-cognition-v9-candidate-admission+epistemic-scope-v1`.
  The separate `BASE_INTERPRETER_VERSION` and `SCOPE_POLICY_VERSION` name the
  implementation and installed wrapper. New commit idempotency and runtime
  manifests use the effective version. Existing historical V4 receipts are
  retained; this gap scan does **not** silently destructively recompute them.
- Fast cognition does not treat another speaker's "You know what, ..." as the character's
  BELIEF and does not automatically adopt externally addressed second-person
  statements as character-owned BELIEF/STATE/RULE/GOAL.
- The live-head cognition barrier is unchanged. After source advance, a
  `message_cognition_catchup` job scans the *perceived ancestors* of the
  current source DAG head, oldest first, for source nodes with no cognition
  commit. An existing zero-unit commit counts as handled.
- Each pass processes at most 32 nodes; a worker invocation runs up to eight
  passes (256 nodes). Source timeline, exact character instance, perception,
  DAG ancestry, current head time bound, and pinned source-head identity are
  checked within the commit transaction. Work uses the
  GLOBAL/BACKGROUND lane with instance partitioning.
- Historical nodes may recover cognitive units, but their GOAL materialization,
  enrichment inference, and polarity supersession are **deferred**. They
  cannot change the current goal or project an older scene over the current
  HUD. Existing live-head behavior remains unchanged.
- Both cognitive-ready and retrieval-ready advancement now require the
  processed node to equal the current source head and cannot decrease the
  recorded cognitive-ready event ID.

## Explicit offline recovery (no new roleplay turn)

**Apply the queue-index migration before restarting workers.** Existing
`ux_pipeline_job_global_active` would otherwise treat all instance-only
catch-up jobs as one global job, incorrectly suppressing other characters.
The migration preserves uniqueness for ordinary global jobs and introduces
`ux_pipeline_job_cognition_catchup_active` keyed by actual instance:

```bash
cd ~/AIOS/aios_app/beta
psql 'postgresql://aios:aios@127.0.0.1:5432/aiosdb_fresh_test' \
  -v ON_ERROR_STOP=1 \
  -f migrations/current/20261003_01_cognition_catchup_queue.sql
```

After pulling, applying the migration and restarting workers, a bounded
manual recovery command is also available:

```bash
cd ~/AIOS/aios_app/beta
python -m aios_app.epistemic.cognition_catchup \
  --instance-id INSTANCE_UUID_HERE \
  --max-batches 8
```

It prints one JSON receipt per bounded batch. Inspect completion by joining
the same source DAG nodes against `message_cognitive_commit` as in the
Experiment 5 diagnosis. The queue-index migration above is required before processing the new job type.

## Second patch: source-owned goal admission and retrospective reconciliation

The second patch is implemented in V9. Generic self-authored commitments can
create managed goals; explicit refusals are recorded as scene positions rather
than automatically becoming action plans. Candidate rejection reasons and the
zero-unit outcome are persisted with every newly committed cognition receipt.

Historical enrichment is bounded and source attributed. The local model's
proposals are reviewed before deferred goals become authoritative. Catch-up
reconciles current-branch evidence in source order, skips older observations
superseded by later intentions, and refuses to overwrite later or terminal
managed goals. If the local inference endpoint is unavailable, completion
returns an explicit `source_inference_unavailable` status instead of silently
promoting unresolved evidence.

The manual command above now also performs bounded enrichment and
historical goal reconciliation after the DAG gaps have been recovered.

See `docs/cognition_goal_admission_v9.md` for detailed diagnostics and
validation queries.

## Focused regression commands (run after the second patch, as planned)

```bash
pytest -q \
  tests/test_cognition_catchup.py \
  tests/test_message_cognition.py \
  tests/test_epistemic_scope.py \
  tests/test_message_cognition_idempotency.py \
  tests/test_hud_readiness.py \
  tests/test_runtime_version_manifest.py \\\n  tests/test_cognition_admission_v9.py \\\n  tests/test_deferred_goal_reconciliation.py \\\n  tests/test_goal_admission_flow_v9.py
```

Version-upgrade replay of existing V4 commits must be a separate opt-in
operation with evidence supersession rules, not a side effect of looking
for previously missing nodes.
