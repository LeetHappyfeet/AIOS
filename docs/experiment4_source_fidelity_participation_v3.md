# Experiment 4 follow-up: source fidelity V3 / shadow participation V3

The Oct 2 experiment f4e2c1b7-627d-4f05-98ed-3d442e623c93 reached 84 claims
after an extra chat turn (initial export: 75). The 84 current receipts were
semantic-integrity-v2-source-comparison / valid. Old message cognition rows for
event IDs 74 and 87 were stamped message-cognition-v4; these are persisted
historical records, not proof that newer workers ran on those events.

## Apply before starting workers

From the AIOS application root, apply the incremental migration **once**:

\`\`\`bash
psql 'postgresql://aios:aios@127.0.0.1:5432/aiosdb_fresh_test' \
  -v ON_ERROR_STOP=1 -f migrations/current/20261002_01_source_comparison_audit.sql
\`\`\`

Restart API, pipeline runner, participation worker and message-cognition workers
to load these constants. New experiments record a runtime_versions manifest at
enrollment, every evaluated participation row stores signals.runtime_versions,
and message-cognition commit summaries capture the loaded component versions.
Previously persisted v4 cognition rows are **not** silently rewritten.

## Behavior

- message-cognition-v8-source-owned scopes fast-path GOAL negation to its own
  predicate and trims a trailing independent clause. Other-speaker requests
  still cannot become the character's self-authored actionable goal. Positive
  self-authored goals remain eligible for normal goal lifecycle processing.
- semantic-integrity-v3-fidelity quarantines Experiment 4 examples involving
  future reported speech without content/modality, figurative metabolic language
  not marked as such, and an adjectival hunted look treated as hunting.
  These conservative checks do not purport to solve all English metaphor.
- source-compare-v2-preceding-context provides an **opt-in** local inference
  discrepancy report using only preceding section context and the source
  sentence. Missing source anchors never substitute later context. A missing
  or invented evidence quote forces an ambiguous verdict.
- claim_source_comparison_audit stores a review against the exact existing
  integrity revision. A concurrent revision change prevents stale persistence.
  Model verdicts and suggested repairs NEVER change semantic admission.
- participation-shadow-v3-scene adds a bounded source-grounded cold-start
  scene-consequence signal (self-authored refusal, direct request, decision,
  situational role claim, and character-related negotiation terms).
  Integrity-invalid/incomplete/unknown claims are not promoted by this path.
  V1 remains the primary ledger and V2 remains unchanged.

## Audit explicit claims with the local provider

This operation is intentionally outside live normalization. After applying the
migration and ensuring the local inference broker is routable:

\`\`\`bash
python -m aios_app.epistemic.source_comparison \
  --instance-id b5bf17a6-fb4a-4e72-b919-8d1b42ad167c \
  --claim-id 5fe7cf7b-deba-44bc-a20c-24933a9f9e31 \
  --claim-id 8d153403-26b5-4f7d-b896-473beec13676 \
  --claim-id 028339c8-e14a-4fd1-a11b-a875cf54daea
\`\`\`

Up to 12 explicit claim IDs can be audited per invocation. The results are JSON
lines and stored in claim_source_comparison_audit. No automatic LLM calls are
added to the latency-sensitive live pipeline.

## Replay original evidence without regenerating the conversation

Restart the API and POST the existing scoped compare endpoint:

\`\`\`bash
curl --fail-with-body -sS -X POST \
  'http://127.0.0.1:8000/agent/instance/b5bf17a6-fb4a-4e72-b919-8d1b42ad167c/participation/experiments/f4e2c1b7-627d-4f05-98ed-3d442e623c93/compare?limit=100'
\`\`\`

The replay fills signals.comparison_v3 on frozen snapshots without modifying
V1 population or existing V2 comparison. V3 fields appear in the existing
experiment report as comparison_v3_version and comparisons_v3. Frozen replay
is explicitly **not** a reconstruction of character state at source-event time.

For new experiments, check runtime provenance:

\`\`\`sql
SELECT experiment_id, runtime_versions
FROM aios.character_participation_experiment
ORDER BY created_at DESC LIMIT 5;

SELECT signals->'runtime_versions',
       signals->'comparison'->>'population' AS v2,
       signals->'comparison_v3'->>'population' AS v3,
       COUNT(*)
FROM aios.character_participation_evaluation
WHERE experiment_id='f4e2c1b7-627d-4f05-98ed-3d442e623c93'
GROUP BY 1,2,3;
\`\`\`

Existing pre-migration experiments retain their original enrollment manifest
as the empty default. A new run is needed to observe genuine v8 cognition,
not simply reclassify v4 persisted units.

## Regression suite

\`\`\`bash
pytest -q \
  tests/test_semantic_integrity_experiment.py \
  tests/test_semantic_fidelity_v3.py \
  tests/test_source_comparison.py \
  tests/test_message_cognition_enrichment.py \
  tests/test_participation_v2.py \
  tests/test_participation_v3.py \
  tests/test_runtime_version_manifest.py \
  tests/test_participation_shadow.py
\`\`\`

This patch is a bounded improvement, not an automatic self-certifying LLM
repair engine. Historical /char snapshots and durable identity promotion remain
separate follow-up work.
