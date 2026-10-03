# Source-grounded integrity development pass (2026-10-02)

Regression basis: Renamon–George shadow export `5df139c4-5ef8-472a-ab6e-41b3033bcbac` (67 evaluated claims). This is an illustrative failure corpus, not an estimated system-wide error rate.

## Admission changes

- `message_cognition.interpret_message` does not adopt second-person requests or other-speaker assertions as character-owned GOAL units. A third party's desire remains testimony in the source pipeline, not an actionable intention.
- `semantic_integrity.validate_frame` now detects several source-to-tuple fidelity failures: unresolved discourse subjects, a future auxiliary extracted as a performed action, lost modal action arguments, and a mismatched actor for explicit regret.
- `claim_semantic_integrity` receipts now use validator version `semantic-integrity-v2-source-comparison`; their revision keys include a SHA-256 digest of the containing source section, while `frame_snapshot` retains the existing list-of-frames contract.
- Existing admission is still fail-closed for invalid/incomplete receipts in `normalizer.normalize_claim_once`. This does not rewrite the historical source or guarantee every valid claim is semantically faithful.

## Optional local verification

`epistemic.source_comparison.compare_claim_with_local_inference(db, claim_id=..., instance_id=...)` is a read-only diagnostic operation using the registered inference broker's `message_cognition` worker class. It supplies only the claim, a bounded window of its original source section, speaker/recipient, and frame data. It returns a verdict, reason codes, an exact source-evidence quote when present, an optional proposed correction, request ID, source-node ID, and section digest.

Inference results are **not** admission certificates. The operation does not mutate goal state, frames, propositions, receipts, or character knowledge. A future controlled repair stage may use reviewed proposals with revalidation, but must never blindly trust model-generated frames.

## Regression commands

```bash
pytest -q tests/test_semantic_integrity_experiment.py tests/test_message_cognition_enrichment.py tests/test_source_comparison.py
```

The GitHub repository connector can commit code but cannot run tests against the user's AIOS database or local inference provider. Run these tests in the AIOS virtual environment. Review downstream existing goals from earlier experiments independently; new admission rules do not retrospectively delete materialized intentions.

## Next experiment

Keep the 67-claim export immutable. Re-evaluate source integrity against the unchanged DAG source, record false admission and false quarantine separately, then compare V1/V2 participation only against corrected admissible representations. Compare source-event time separately from extraction, cognition acquisition, and experiment evaluation time. Do not infer historical decisions from evaluation-time snapshots.
