# Message Cognition V11: source-bound managed-goal admission

V11 separates detecting a possible intention from establishing an authoritative
managed goal. `message-cognition-v11-source-goal-admission+epistemic-scope-v1`
is a distinct interpreter version; old V10 receipts remain identifiable.

## Deterministic policy

`epistemic/goal_source_admission.py` is an inference-free guard. Given the
original clause, proposed objective, parser match span and horizon, it returns
an admission decision and reason. It checks the preceding and following
narration for embedded, imagined or figurative first-person statements, rejects
bare unresolved objective references, and routes trivial immediate physical
actions away from the session goal lifecycle. This is intentionally
conservative: uncertain candidates remain in `candidate_rejections` with
their source excerpt instead of inventing a goal. The original DAG message
remains available for scene processing and any future bounded review.

The V11 parser persists `source_span`, `goal_admission_version`, and
`goal_admission_status` on admitted GOAL units. It retains the existing
epistemic scope wrapper and its distinct version. `CharacterGoalService.
reconcile_evidence` rechecks source-bound admission when the caller supplies
source text, and both live source projection and historical reconciliation pass
this source provenance. Enrichment applies the shared policy before admitting
LLM-proposed GOAL units and supplies the excerpt to the managed goal writer.

No additional LLM call runs on ingestion. The existing read-only
`source_comparison.py` is a reference for any *later*, opt-in, cached
goal-specific discrepancy audit. It must not be made a second authority.

## Observed regression corpus (generic names)

- Direct `"I'm staying two nights."` remains a session commitment.
- A gesture or relocation `that said I will put this down...` is
  narrator-implied first-person language, **not** owned speech.
- `"I'll put my bag down in a minute."` is scene-only work.
- `"I'm going to start collecting them."` is an unresolved objective
  until a specific referent is grounded. The policy does not guess it.
- Direct `"I will stay until Friday."` remains eligible, whereas
  imagined or post-attributed speech does not.
- An old historical V10 unit with the same nonliteral source is rejected
  during goal reconciliation even if its GOAL unit has already been stored.

## Historical-state caution

Changing the interpreter version does **not** silently delete or relabel
existing managed goals. An existing goal may have an open attempt, immutable
outcome history, or dependent temporal trigger. Audit old source provenance
and, if a correction is supported, use explicit goal lifecycle controls with
a recorded resolution; do not rebuild the goal table or mutate closed attempts.

## Limits / later follow-up

The first pass does not perform an open-ended antecedent search or automatically
rewrite unresolved pronouns into a new objective. A future bounded resolver may
inspect the exact previous DAG source span and admit only an unambiguous
referent with retained evidence offsets. An optional LLM source comparison
should remain diagnostic and may only propose a correction that deterministic
source verification subsequently checks.
