# Experiment 5 — source-DAG cognition catch-up (first of two patches)

This patch responds to experiment
\`9d8bdc27-2e2e-44a2-a4f3-ca666830c592\`,
Renamon instance \`cce90979-aac6-4333-b2b9-772d73158a5b\`.
Events **9–14** have ordinary extracted claims but no
\`message_cognitive_commit\`; events 15, 30 and 31 do have V4-stamped commits.
The V4 stamp was caused by \`install_message_cognition_scope_guard\` resetting
the effective version on top of the V8 implementation.

## Changes

- The effective interpreter version is now
  \`message-cognition-v8-source-owned+epistemic-scope-v1\`.
  The separate \`BASE_INTERPRETER_VERSION\` and \`SCOPE_POLICY_VERSION\` name the
  implementation and installed wrapper. New commit idempotency and runtime
  manifests use the effective version. Existing historical V4 receipts are
  retained; this gap scan does **not** silently destructively recompute them.
- Fast cognition does not treat George's "You know what, ..." as Renamon's
  BELIEF and does not automatically adopt externally addressed second-person
  statements as character-owned BELIEF/STATE/RULE/GOAL.
- The live-head cognition barrier is unchanged. After source advance, a
  \`message_cognition_catchup\` job scans the *perceived ancestors* of the
  current source DAG head, oldest first, for source nodes with no cognition
  commit. An existing zero-unit commit counts as handled.
- Each pass processes at most 32 nodes; a worker invocation runs up to eight
  passes (256 nodes). Source timeline, exact character instance, perception,
  DAG ancestry, and current head time bound are checked. Work uses the
  FAST_SQL/BACKGROUND lane with instance partitioning.
- Historical nodes may recover cognitive units, but their GOAL materialization,
  enrichment inference, and polarity supersession are **deferred**. They
  cannot change the current goal or project an older scene over the current
  HUD. Existing live-head behavior remains unchanged.
- Both cognitive-ready and retrieval-ready advancement now require the
  processed node to equal the current source head and cannot decrease the
  recorded cognitive-ready event ID.

## Explicit offline recovery (no new roleplay turn)

After pulling and restarting the pipeline workers, a bounded manual recovery
command is also available:

\`\`\`bash
cd ~/AIOS/aios_app/beta
python -m aios_app.epistemic.cognition_catchup \
  --instance-id cce90979-aac6-4333-b2b9-772d73158a5b \
  --max-batches 8
\`\`\`

It prints one JSON receipt per bounded batch. Inspect completion by joining
the same source DAG nodes against \`message_cognitive_commit\` as in the
Experiment 5 diagnosis. No migration is required for this first patch.

## Deliberately reserved for patch two (before the next experiment)

Do NOT interpret successful gap recovery as proof that goals are repaired.
The next patch is the separately proposed goal lifecycle and admission pass:
audit actual Renamon-authored candidates, differentiate explicit goals from
refusals and conditional adoption, make rejection reasons visible, and
chronologically adjudicate the historical \`goal_projection_deferred\` and
\`enrichment_deferred\` records before checking authoritative
\`character_agent_goal\`, participation context, and HUD retrieval.

Also distinguish successfully processed zero-unit messages from skipped
messages; a zero-unit receipt is legitimate but should carry candidate
rejection diagnostics in the next patch.

## Focused regression commands (run after the second patch, as planned)

\`\`\`bash
pytest -q \
  tests/test_cognition_catchup.py \
  tests/test_message_cognition.py \
  tests/test_epistemic_scope.py \
  tests/test_message_cognition_idempotency.py \
  tests/test_hud_readiness.py \
  tests/test_runtime_version_manifest.py
\`\`\`

Version-upgrade replay of existing V4 commits must be a separate opt-in
operation with evidence supersession rules, not a side effect of looking
for previously missing nodes.
