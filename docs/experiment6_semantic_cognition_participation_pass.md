# Experiment 6 repair: source coverage, owned cognition and role-separated participation

This pass extends the branch after the two cognition-catchup/goal-lifecycle
patches. It does not change the historical V1/V2/V3 participation decisions.
It does not assert that an LLM-proposed correction is authoritative.

## Changes

- **Integrity V4, source coverage** (\`semantic-integrity-v4-source-coverage\`):
  conservative receipts for lost future, conditional, ability, directive,
  duration and location information; missing intention arguments; and the
  ambiguous "fell through" idiom. Numeric duration tokens are preserved.
  The entire containing section still participates in the revision digest.
  Unsupported representations are marked \`incomplete\` (or \`invalid\`
  for a provably wrong auxiliary-action substitution), never auto-corrected.
- **Message Cognition V10**: a character-authored time/duration-bounded
  progressive such as "I'm staying two nights" becomes an explicit, managed
  commitment candidate. Generic ongoing progressive prose does not. Elliptical
  preferences ("I'd just prefer to know tonight") reach bounded enrichment
  rather than producing a goal with an invented argument. Source-owned
  requirements, optional inference, historical replay and terminal-goal
  safeguards from V9 remain in place. Versioned receipts intentionally
  distinguish new V10 processing from existing V9 records.
- **Participation V4** (\`participation-shadow-v4-source-roles\`): additional
  *shadow-only* comparator explicitly distinguishes narrator, claim actor,
  target, witness (unknown unless grounded) and self-adopter. An external
  proposed arrangement may warrant foreground consideration, but adoption
  remains \`unconfirmed\`. Environment details narrated by a character no
  longer count as direct involvement by virtue of source authorship alone.
  V1 is retained as the unchanged comparison baseline; V2 and V3 continue to
  be written alongside V4.
- **Historical temporal honesty**: each new participation context notes
  whether its claim is the current runtime source head. V4 deliberately
  suppresses present-day goal/facet/relationship relevance for retrospective
  claims lacking a reconstructed as-of snapshot, adding
  \`historical_relevance_not_reconstructible\`. It may still evaluate
  source-grounded local events but does not invent historical relationship or
  goal membership. The existing V1-V3 decisions remain comparable.
- **Stage diagnostics**: \`epistemic/cognition_diagnostics.py\` is read-only,
  tracing current source-head ancestors through cognition receipts, zero-unit
  outcomes, owned GOAL units, bounded enrichment, deferred reconciliation,
  managed goals, HUD readiness and (optionally) the experiment's evaluation
  snapshots. It describes the first observed pipeline blocker without
  automatically replaying or admitting old evidence.

## Deployment and diagnostics

After pulling the branch, restart the API, normalizer, pipeline and cognition
workers to load the new constants. Run the migration runner normally; this
pass adds no new SQL table or incremental migration.

\`\`\`bash
cd ~/AIOS/aios_app/beta
git pull origin AIOS-development

python -m aios_app.epistemic.cognition_diagnostics \
  --instance-id INSTANCE_UUID \
  --experiment-id EXPERIMENT_UUID
\`\`\`

For an existing experiment, V1/V2/V3 evaluation receipts are intentionally
frozen. The existing paired-compare endpoint can fill previously missing V4
comparison fields using those saved source/context snapshots. It cannot
retroactively invent goals or rewrite prior V9/Integrity V3 receipts. A
**fresh, character-agnostic source replay** is required to actually compare
V10 and Integrity V4 end to end. Pair future comparisons on the same source
DAG nodes instead of comparing two differently generated stories.

Interpret source coverage and goal continuity separately: a current-branch
source with no cognition receipt indicates a dispatch/gap problem; a valid
zero-unit receipt with meaningful rejected candidates suggests parser
admission; positive owned GOAL units but no managed goal indicates
enrichment/reconciliation/lifecycle timing or terminal-state preservation.
A nonempty managed-goal table with empty historical participation snapshots
is not necessarily an admission failure: the timing of the evaluation
matters. Use the diagnostic output rather than foreground share alone.

## Focused tests

\`\`\`bash
pytest -q \
  tests/test_experiment6_fidelity_v4.py \
  tests/test_cognition_v10_temporal_intention.py \
  tests/test_participation_v4_source_roles.py \
  tests/test_cognition_diagnostics.py \
  tests/test_semantic_integrity_experiment.py \
  tests/test_runtime_version_manifest.py \
  tests/test_goal_admission_flow_v9.py
\`\`\`

The full fast-cognition GitHub Actions workflow also exercises
historical-reconciliation, existing admission, HUD and runner tests.

## Known remaining boundary

The adapter does not claim to solve full sentence-level semantic *recall*
across separately materialized subclaims, nor does it reconstruct historical
character state from the current database. That requires a source-section
coverage audit and an as-of snapshot or historical event replay. Scene changes
affecting a character through an indirect dependency require explicit
scene-state provenance; merely matching an entrance verb is not sufficient
to claim a foreground impact.
