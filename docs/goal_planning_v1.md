# Goal planning under incomplete information

An active goal can now carry an advisory strategy, up to three tentative steps,
and up to three typed information gaps. The foreground HUD displays this plan
beside the managed goal instead of the generic toolkit footer. Missing local
resources stay unknown: a procedural reference cannot establish refrigerator
contents, and no plan creates world entities or action outcomes.

## Runtime

The independent transaction scheduler offers the primary active goal of each
unpaused agent one `planning.propose` operation. It does not wait for the normal
chat cognitive batch threshold. Admission rotates across instances and allows
only one queued plan / outstanding plan-owned operation per instance. It uses
the existing isolated inference job mechanism (`goal_planning_inference`,
background lane) and the configured `planning` inference provider.

The prompt contains the admitted objective, at most four chronological source
messages on the current timeline through the source head, up to three already
goal-linked character evidence items, knowledge requirements, information receipts, and the
previous proposal. Long messages preserve both their opening and their ending.
Source text is labeled as reported material, not independent world truth.

The model returns only `strategy`, `steps`, `open_questions`, `next`, and
`evidence_ids`. Question kinds are `personal`, `procedure`, and `environment`.
Next-step categories are `internal_lookup`, `research`, `request_information`,
and `wait`. Validation enforces bounds, allowed fields and evidence IDs, and
forbids routing the first environmental question into a reference lookup.
Free text remains advisory; these checks do not prove that model wording is
factually grounded. Proposals never enter semantic ingestion or acquire belief
or world authority.

## Continuation and stopping

One proposal can dispatch at most one host-constructed information operation.
A stable information target is retained per goal attempt to prevent query wording
drift from opening repeated dossiers. It reuses character inquiry first, then bounded progressive research when an
earlier inquiry did not establish support. Existing no-access and dossier
availability/backoff rules apply. Personal questions do not escalate into
progressive reference research. Environmental questions wait for supplied
information; this version adds no simulator, sensor, or physical action adapter.
Research exposure is not study or acquisition. There is no automatic study.

Deduplication uses the goal attempt, objective, source timeline/head, requirement
coverage, bounded information receipt summaries, and relevant corpus/access epoch after a no-access result. It
does not include planning timestamps or proposal revisions. Identical inputs
(including failed requests) do not trigger another model call. Relevant later
chat messages, changed requirements/receipts, or a new corpus/access epoch can
permit a new revision. Polling itself performs no inference on unchanged inputs.

Plan-owned operations do not acknowledge host cognition deltas or record goal
progress. Waiting for information finishes the operation; it does not keep a
cognitive episode open. Existing operation recovery cleans up failed/stale work.
Goal attempt, timeline/head, input fingerprint, and live operation
ownership are checked before accepting model output. Invalid or stale responses
preserve the preceding accepted plan. The HUD labels earlier-context plans.
Closing or deactivating a goal closes its visible plans; new attempts never
inherit an old attempt's plan. There are no planning writes during HUD assembly.

## Deployment

Stop old processes, pull `AIOS-development`, run `python -m aios_app.migrate`,
and restart services. Apply immutable migration
`20261014_02_goal_planning.sql` and `20261023_01_goal_review_repair.sql`; no reset or reingestion is required. Mixed old
and new workers are not supported. A configured planning provider is required
for proposals. Unavailable providers defer the existing planning job with bounded backoff.

Inspect proposals and their inference operation:

```sql
SELECT p.goal_id,p.attempt_id,p.revision,p.status,p.source_node_id,
       p.proposal,o.status AS operation_status,o.result
FROM aios.character_goal_plan p
LEFT JOIN aios.character_cognitive_operation o USING(operation_id)
WHERE p.instance_id='<INSTANCE_UUID>'::uuid
ORDER BY p.created_at DESC;
```

Focused tests: `tests/test_goal_planning.py` and
`tests/test_goal_planning_postgres.py`. PostgreSQL tests use the existing
isolated PGlite bridge and never contact the application database or a model.

## Queue identity correction

Migration `20261014_03_cognitive_job_identity.sql` removes cognitive jobs from
the legacy global job-type singleton index. Active work is deduplicated by
operation, transaction, task, or instance identity, while true global jobs
remain singletons. Distinct operations can coexist even within one instance.

This fixes `could not enqueue goal planning` during and after startup. The
queue had silently rejected distinct jobs through `ON CONFLICT DO NOTHING`.
The migration requeues only the latest plan per instance whose operation
failed with that exact enqueue error, preserving the error in
`input.enqueue_repair`. Existing recovery supplies its missing queue job.
Initial admissions that rolled back are retried by the normal scheduler.
Other model, validation, or stale-context failures remain terminal.

Stop services, pull, migrate, and restart; no manual goal reset is needed.
`tests/test_cognitive_job_identity.py` reproduces the legacy collision and uses
the real enqueue SQL and both index versions to verify the correction.

## Bounded plan context

Plan preparation no longer calls `CognitiveContextService.resolve_knowledge` or
builds foreground attention. Those paths traverse memory, belief, goal, event,
and rule topology, use world/semantic fallbacks, and may do supplementary corpus
work. A small plan must not repeat that generation-time retrieval pipeline.

The planner reads at most eight proposition IDs already recorded on its goal's
knowledge requirements and returns at most three currently owned character
records. The point lookup rechecks acquisition receipts and source supersession,
uses the existing permitted character lineage, and has a 750 ms deadline. With
no linked IDs it performs no memory query. Timeout returns `unavailable`, not
ignorance. A prompt always states that this evidence subset is not a complete
memory search. Explicit inquiry/research remains the route for deeper questions.

Logs now include `Goal planning context` with source-message count, linked
knowledge count, knowledge status, and context preparation duration. This change
requires a code update and service restart, but no additional migration beyond
the queue correction above. Independent semantic-index SQL/lock contention may
still require separate investigation.


## Source review and planning repair

Goal reviews use a dedicated bounded prompt containing up to four source messages,
with opening, ending and lexically relevant middle passages. Each passage carries
its original character offsets and source node, speaker and role. Before inference,
original messages are reloaded under the operation's source timeline/head and checked
against the stored hashes. Legacy queued reviews are reloaded too. The micro-HUD
subject limit no longer removes this evidence. Reports remain reports; the prompt
separates the goal owner, speaker and intended performer, attempts, progress and
satisfaction. It does not promote narration to independently verified success.

A review revision includes the attempt, goal text, source coordinates, message
hashes (including attribution), and the latest non-review goal evidence ID.
Successful or outstanding reviews of the same revision are suppressed. Failed/stale
reviews have a 60-second retry hold. An advisory lock and partial unique index
protect concurrent admissions. A changed attempt, source or external evidence can
permit a new review; a review's own progress receipt cannot cause another review.
Completion reviews retain strict state/head checks and the lifecycle's later-source
and live-attempt guards. This release does not rewrite existing goal statuses.

Advisory plan fingerprints exclude the generic runtime state version. Source
changes, different attempts, changed knowledge inputs and operation ownership
still invalidate work. `character_goal_plan.input_context` retains the components
used for the fingerprint. Stale operation results now record `reason`, `stage`
(`before_inference`/`after_inference`), `changed_inputs` and, when available,
`inference_request_id`. A state-only change no longer makes the HUD label an
otherwise current advisory plan as outdated.

The planner checks routable provider capacity before admission. A race with capacity
or a later provider outage defers the same operation and pipeline lease, preserving
attempt identity and the preceding accepted plan. Backoff increases from 15 seconds
to a 300-second maximum, recorded in the operation result. The runner uses its
existing ownership-checked deferral rather than consuming queue failure attempts.
HTTP 429 responses immediately set a shared provider cooldown (15–300 seconds,
using numeric Retry-After when supplied); they do not declare the endpoint offline.
Provider errors retain bounded JSON error details with configured credentials
redacted, so HTTP 400 failures can be diagnosed without weakening plan validation.

Inspect the new diagnostics with:

```sql
SELECT operation_type,status,result->>'reason' AS reason,
       result->>'stage' AS stage,count(*)
FROM aios.character_cognitive_operation
WHERE operation_type IN ('planning.propose','planning.review')
GROUP BY operation_type,status,result->>'reason',result->>'stage';
```

Regression tests also cover bounded evidence reaching the actual transaction prompt,
source/attempt changes, duplicate review admission, unavailable providers, rate-limit
cooldown, and state-only changes during inference. Run the cognitive-episode-recovery
workflow, including its isolated PostgreSQL fixtures, before deployment.
