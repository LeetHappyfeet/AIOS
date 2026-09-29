# Character outcomes and shadow appraisal

Goals own intention. Outcome resolution records consequences. Reinforcement derives
versioned observational signals. No scheduler, opportunity scorer, HUD prompt, or
executive decision consumes those signals in this release.

## Lifecycle and persistence

Terminal statuses are completed, failed, and cancelled. Scheduled/dormant goals remain
unresolved. A missed-opportunity planning review produces **reviewed failure**;
withdrawal produces cancellation. A scene contract yields a **projected** outcome.
Neither conclusion alone earns strategy credit. Operational failure is not goal failure.

Migration 20260929_05 installs atomic goal triggers: inserting a live goal opens an
attempt; terminal status closes it, records an outcome, cancels scheduled/firing timers,
resolves goal threads, dirties HUD readiness, and queues appraisal in the same transaction.
These triggers also cover connection-bound message cognition and existing SQL callers.
No historical cancelled/completed rows are relabelled or given invented rewards.

Contract and expectation snapshots are frozen. `goal_contract.success/failure` accepts
the existing slot/path/operator/value format; legacy completion_contract and
failure_contract remain supported. Missing slots, mismatched scalar types, and conflicting
success/failure matches cannot resolve a goal. Matching remains source-timeline local.
Changing a live contract requires a new goal. Deferral and reactivation keep the attempt;
`CharacterGoalService.retry(request_id=...)` opens a new attempt explicitly and idempotently.
Retries and inherited goals do not reuse old prediction expectations automatically.

## Authorized producers

`OutcomeResolver.resolve_receipt()` requires a newly acquired, frozen typed receipt:

```json
{
  "outcome_receipt": {
    "goal_id": "uuid",
    "attempt_id": "uuid",
    "outcome": "success",
    "attribution": "self",
    "cause_class": "achieved",
    "strategy_receipt_id": "uuid"
  }
}
```

The acquisition must belong to the instance/attempt timeline, postdate attempt admission,
and have deterministic_tool origin, canonical authority, and action_precondition permission.
Confidence or a caller-supplied verified flag is insufficient. Receipt content is captured
on acquisition INSERT; later metadata edits cannot rewrite it. A generic canonical fact or
external delivery queued receipt is insufficient. Producers must establish the actual
outcome before emitting this format. Existing narrative/corpus producers are not promoted.

The supervisor handles bounded receipt intake, records rejected receipts, and drains the
durable appraisal inbox independently of autonomy scheduling. Unexpected database errors
leave work pending for retry. A delayed receipt can correct its own old attempt but cannot
close a new retry attempt. Corrections append successors and leave original records intact.

## Shadow strategy appraisal

The host may call `OutcomeResolver.link_strategy()` while an attempt is open, with a
registered strategy key and an actually executed action/cognitive operation tied to that
goal. V1 permits one association per attempt. It is not model-visible and is not inferred
from every preceding operation. The outcome receipt must explicitly credit that same
execution ID; attribution to self alone does not establish strategy causality.

Only verified success/failure with self attribution and a matching terminal strategy
receipt yields strategy feedback. External obstruction, mixed/unknown attribution,
withdrawal, unverified reviews, and absent strategy receipts produce zero-credit audit events.

Optional `meta.expectation` at goal admission contains success_probability and importance,
finite numbers in [0,1]. With a prior probability, the signal is
`0.25 * importance * (observed_success - success_probability)`.
Without one, ordinary signed outcome feedback is recorded; no surprise is fabricated.
Importance defaults to 0.5. Magnitude is bounded by 0.25 in either direction.

Migration 20260929_06 adds immutable reinforcement events and rebuildable projections of
success/failure counts and mean signals, scoped by instance and source timeline. Policy
version is reinforcement-shadow-v1. Superseded outcomes are excluded during rebuild.
No global dopamine/motivation balance, emotional-state changes, or decision bonuses exist.
This is an audit substrate, not LLM training or a claim of biological dopamine simulation.

## Operator inspection and correction

- GET `/agent/instance/{instance_id}/reinforcement?limit=50`: attempts, outcomes (including
  effective flags), signals, projections, pending appraisal count, and policy version.
- POST `/agent/instance/{instance_id}/goal/{goal_id}/retry`: body `{ "request_id": "uuid" }`.
- POST `/agent/instance/{instance_id}/outcome/{outcome_id}/invalidate`: body
  `{ "request_id": "uuid", "reason": "receipt retracted" }`. Appends an unresolved
  correction; next appraisal rebuild removes old credit. It does not rewrite world facts
  or silently reopen the goal.

These routes follow the existing operator API deployment/access model. Authorized receipt
admission and strategy association are host APIs, never character actions. Evidence
retractions should use the correction API; this release does not infer corrections from
arbitrary world changes or merge learning between instances.

## Validation

The character-outcomes workflow runs PostgreSQL 16 transaction/race tests and lifecycle
regressions. For local tests set AIOS_OUTCOME_TEST_DSN to a server whose role can create
and drop temporary databases; tests never modify an application database. Run the standard
AIOS migrations before launching the updated supervisor. The existing baseline remains
unchanged; both new migrations are applied through the normal migration ledger.

Cognitive operations are stamped with goal_attempt_id at INSERT. Late reviews from an old
attempt remain telemetry; they cannot finish a new retry or contribute its evidence.
Terminal updates additionally compare the expected open attempt inside the atomic SQL
mutation, covering a retry racing with an already-running resolver.
