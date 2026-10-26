# Goal lookup continuation and corpus assignment

Apply canonical migrations (`python -m aios_app.migrate`) before starting the
updated workers/API. Migration `20261025_01_goal_dependency_continuation.sql`
adds continuation diagnostics, a due index, catalog change tracking and contextual
lookup traces. Migration `20261025_02_continuation_lookup_worker.sql` adds
recoverable continuation leases and the dedicated LOOKUP resource class. Apply
both through the canonical migrator before starting updated workers.
Existing waiting plans are eligible for a bounded continuation
check; closed attempts, paused runtimes and old source heads cannot dispatch work.
The next normal planning poll can propose a current plan after chat advances.

## What changes

An accepted advisory plan commits before optional lookup admission. The supervisor
only enqueues continuation jobs, even without a routable planning inference
provider. A dedicated LOOKUP worker (one by default, configurable with
`AIOS_RUNNER_LOOKUP_WORKERS`) runs these BACKGROUND jobs independently of GLOBAL.
It claims a 90-second fenced lease, persists requirements in a short transaction,
releases all goal/runtime/plan row locks for discovery, then reacquires locks and
revalidates lease, plan, attempt, source head and timeline before child admission.
A single worker discovery phase is capped at 25 seconds; each revision check keeps
its seven-second deadline. Pending child work skips redundant discovery.
Expired leases can be reclaimed, and a late worker cannot commit after takeover.
A durable request ID is recorded on completion so retrying the same queue request
cannot admit another child operation even if its earlier child has already finished.
Paused work backs off; closed attempts and outdated sources cannot admit work. Each
of up to three questions gets a durable requirement before RDF revision discovery.
Transient failure backs off 60 seconds and preserves the plan. New inference is
not required to retry the lookup. A failed child enqueue rolls back child admission
and records `child_admission_failed` on that dependency; its information need and
independent questions survive. A failure of the whole continuation records
`continuation_failed` on the accepted plan.

Requirement keys include the goal attempt and a normalized question identity.
Close wording reuses existing same-kind requirements; separate prerequisites
retain separate histories. Previously stored single-slot plan requirements are
retained for audit; the new continuation uses the new keys. It does not rewrite or
transfer ambiguous old inquiry results to another dependency.

Environment and personal questions use internal recall only. An environment
question with no new evidence becomes `awaiting_observation`; it cannot research
local inventory. Procedural questions first run scoped inquiry and can then advance
a bounded research dossier. Pending work is deduplicated by requirement. A failed
or unavailable inquiry receives a distinct retry revision, so the inquiry cache
cannot pin it to a failed receipt. Source/attempt checks still apply on execution.
General goal research is no longer suppressed by the mere existence of a plan;
only overlapping active requirement work is suppressed.

Continuation is advisory. Reference search is not study, possession, skill gain,
physical execution, outcome verification or goal completion. The text HUD shows
lookup status, conditional observation options and reference review guidance.
No new comparison inference or autonomous study is introduced.

## Repairing a cooking corpus

A crawl's declared knowledge domain continues to classify only its seed page.
Descendants need their own structured routing or an operator-created trusted
source profile. This restriction is preserved.

In **Knowledge domains → Dedicated sources**, a URL now preserves its path prefix
instead of silently widening to the whole host. Use a dedicated recipe host/path
and the intended domain, such as `fiction.skills`. Existing profiles can be
previewed by entering their profile key and clicking **Preview existing profile
classification**. **Add source profile and classify matching documents** applies
that explicitly configured host/path route to matching stored documents. Do not
use a broad shared-host profile for an unrelated collection.

For an exact operator-selected subset, the catalog service also supports:

```python
catalog = CorpusCatalogService(db)
preview = await catalog.reclassify_profile(
    "cooking-recipes", dry_run=True, document_ids=selected_document_uuids)
# Inspect preview before applying the intended selection.
result = await catalog.reclassify_profile(
    "cooking-recipes", document_ids=selected_document_uuids)
```

The subset must still match the enabled profile's source, host and path checks.
An empty subset changes nothing. Preview performs no writes; apply is atomic.
No content is re-scraped, no character grant is expanded, and current SQL scope
denies continue to apply. Profile and document membership changes advance the
catalog retry epoch. Indexing and character-grant epochs are still included.
This release does not guess which production documents belong to the cooking
collection or automatically change an existing production assignment.

## Diagnostics

```sql
SELECT goal_id, revision, status, continuation_check_at,
       jsonb_pretty(continuation) AS continuation
FROM aios.character_goal_plan
WHERE instance_id = '6bdf24eb-5c5e-4e22-816a-cc1f824beb36'
ORDER BY created_at DESC
LIMIT 10;

SELECT updated_at, source_node_key, diagnostics
FROM aios.character_corpus_lookup_trace
WHERE instance_id = '6bdf24eb-5c5e-4e22-816a-cc1f824beb36'
ORDER BY updated_at DESC
LIMIT 10;
```

Contextual traces record routing, query, status and counts even if search times
out before creating a research event. They omit full source messages, upsert by
instance/source head/query and are best effort. Trace failure cannot block normal
cognition. RDF revision failures now log the exception type and stack, rather than
silently returning an unavailable revision.

## Verification

The isolated PostgreSQL/PGlite tests cover production revision SQL failing before
admission, retry without inference availability, independent dependencies, scoped
no-access receipts, failed enqueue rollback, stale source heads, preview/apply
host/path boundaries, catalog retry epochs, current scope denies, and contextual
search timeout traces. Retrieval/render tests retain the no-acquisition boundary.
