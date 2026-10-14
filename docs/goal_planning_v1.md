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
messages on the current timeline through the source head, up to three relevant
character evidence items, knowledge requirements, information receipts, and the
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

Deduplication uses the goal attempt, objective, source timeline/head and state version, requirement
coverage, bounded information receipt summaries, and corpus/access epoch. It
does not include planning timestamps or proposal revisions. Identical inputs
(including failed requests) do not trigger another model call. Relevant later
chat messages, changed requirements/receipts, or a new corpus/access epoch can
permit a new revision. Polling itself performs no inference on unchanged inputs.

Plan-owned operations do not acknowledge host cognition deltas or record goal
progress. Waiting for information finishes the operation; it does not keep a
cognitive episode open. Existing operation recovery cleans up failed/stale work.
Goal attempt, timeline/head, state version, input fingerprint, and live operation
ownership are checked before accepting model output. Invalid or stale responses
preserve the preceding accepted plan. The HUD labels earlier-context plans.
Closing or deactivating a goal closes its visible plans; new attempts never
inherit an old attempt's plan. There are no planning writes during HUD assembly.

## Deployment

Stop old processes, pull `AIOS-development`, run `python -m aios_app.migrate`,
and restart services. Apply immutable migration
`20261014_02_goal_planning.sql`; no reset or reingestion is required. Mixed old
and new workers are not supported. A configured planning provider is required
for proposals; provider failures remain failed operation receipts.

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
