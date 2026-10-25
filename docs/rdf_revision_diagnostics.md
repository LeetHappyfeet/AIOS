# Seven-second revision checks and query-plan diagnostics

Background `RDFRecallService.discovery_revision()` now has one seven-second
window covering connection acquisition and both SQL reads. The source read and
atom read share one session and transaction; they do not each receive seven
seconds. Cancellation still propagates and releases the session. Ordinary
interactive recall and cached-reference validation keep their existing 350 ms
SQL deadline. Fuseki transport deadlines are unchanged.

Expected deadline expiry emits one concise warning with the instance, stage and
elapsed time. Unexpected errors retain a traceback. Checks slower than 350 ms
emit an informational timing summary. Planning continuation records also carry
`revision_check`, including stage, budget, elapsed time and completed stage times.
The timing fields themselves need no schema change. The current background
worker requires migration `20261025_02_continuation_lookup_worker.sql` after the
previous continuation migration; use the canonical migrator before starting it.

## Investigation findings

The deployed timeout traces identify PostgreSQL reads, not Fuseki requests.
Several acquired-proposition reads and at least one source read exceeded the old
350 ms window. They do not establish a slow-query execution plan or identify a
missing production index.

Code inspection shows:

* Revision detection reuses the full retrieval queries and fetches source text
  and projection payloads. Its `LIMIT 12` bounds returned rows, not the work
  needed to find, authorize and order those rows.
* Both queries traverse active DAG ancestry. The recursive UNION deduplicates
  `(node_id, depth)` pairs, so a node reached at different depths can still appear
  more than once. Actual impact depends on branch topology.
* The atom query does case-folded substring matching over concatenated proposition
  fields. The owner/proposition index helps ownership joins; it is not a matching
  expression index for that substring condition.
* Admission rechecks call `semantic_acquisition_source_eligible()` for candidate
  acquisitions. That SQL function includes further integrity and occurrence
  eligibility lookups. Their actual loop counts and costs need EXPLAIN ANALYZE.
* Previously, per-query `wait_for` tasks did not reuse the transaction-owner
  task's connection. The background revision reader now creates one timed task
  whose SQL calls reuse its transaction connection.

The seven-second deadline remains. The atom query now materializes owner,
proposition/topic, active admission and authority candidates before invoking
source eligibility. It has no preliminary candidate limit. All eligibility,
supersession and branch checks, the final result limit and fingerprint inputs
remain in force. Both discovery and reference validation use this query.

Continuation discovery now runs as a leased BACKGROUND job on a dedicated LOOKUP
worker rather than in the supervisor. Goal/runtime/plan row locks are released
for discovery. Admission rechecks the lease token, active attempt and exact source
head/timeline under short locks. See `goal_dependency_continuation.md` for the
required worker migration and recovery behavior.

## Collect the actual plans

From the same activated environment and package-parent directory used to run
AIOS (so it loads the same database configuration):

```bash
python -m aios_app.entity_memory.diagnostics \
  --instance-id 642c42ba-8378-4dfe-930a-8f67355729eb \
  --query "What is the layout and nature of the workspace where the installation occurs?" \
  --analyze
```

That instance/question came from a reported unavailable environment dependency.
For the cooking instance, repeat with its instance ID and current cookbook
question. Without `--analyze`, the command only requests estimated plans.

The diagnostic resolves the current character/branch coordinates, binds the same
parameters and runs the exact source and atom SQL. It reports index definitions,
approximate table row counts, plans and per-stage elapsed times. `--analyze` adds
actual execution times, loops and buffer statistics. All diagnostic queries run
in a read-only transaction, with a 30-second overall budget and one-second lock
wait limit. `--timeout` can select a different overall diagnostic budget up to
120 seconds. The diagnostic does not invoke inference, Fuseki, study or writes.
Its separate budget does not change the application's seven-second deadline.

Check source versus atom execution time, recursive ancestry row counts, sequential
scans, filter removals, nested-loop counts, sort work and buffer reads. Compare a
cold launch with a warmed run. Actual production plans are required before
claiming which cost dominates or choosing a new index.

## First captured PostgreSQL 16.11 plan

The captured report's source plan executes in 2.1 ms. Its atom plan executes in
310.659 ms (328.8 ms elapsed including planning/transport), returns zero rows,
and does not execute the ancestry CTE. The owner index finds 55 acquisition
candidates in 0.042 ms. The acquisition bitmap heap scan, whose filter calls
`semantic_acquisition_source_eligible(acquisition_id)`, takes 307.994 ms and
returns 51 rows. That scan reports 9,379 shared-buffer hits and 653 reads,
including its function work. Topic matching happens later and removes the
remaining candidates. This sample points to early eligibility work, not ancestry
or a missing owner index, as the immediate cost concentration. It does not explain
every seven-second timeout under concurrent startup load.

This captured legacy plan motivated the materialized candidate stage now in the
atom query. The comparison mode below measures its actual effect on your host;
these legacy timings are not claims about the optimized query's production speed.

The launch log also exposed a separate inquiry fallback defect: RDF failure or
an empty result led to character knowledge retrieval with `scorer=None`, which
then called `scorer.score()`. Inquiry now supplies a query-focused scorer, and
the optional-scorer knowledge API initializes its default when omitted. This fixes
the AttributeError without changing source-access SQL or the seven-second window.

## Compare the optimization on the same database snapshot

Add `--compare` to the diagnostic command:

```bash
python -m aios_app.entity_memory.diagnostics \
  --instance-id 642c42ba-8378-4dfe-930a-8f67355729eb \
  --query "What is the layout and nature of the workspace where the installation occurs?" \
  --analyze --compare > rdf-plan.json 2> rdf-plan.err
```

This explicitly executes both legacy and optimized atom SELECTs, and requests
both plans. It uses one read-only REPEATABLE READ snapshot and reports timings,
coordinate hashes covering every returned column, and `comparison.equivalent`.
No full source text is copied into the comparison report. The extra legacy work
is diagnostic only; normal application queries do not execute both versions.
Repeat on representative relevant and irrelevant questions before claiming a
particular production speedup. Point-lookup equivalence is also covered by the
SQL regression tests. Equal-time duplicate acquisitions can
have unspecified tie ordering in the legacy query; review any reported difference
rather than treating the comparison as authorization to expose extra evidence.

## HUD corpus deadlines are separate

A `Contextual corpus lookup` traceback from `CognitiveContextService` is the
interactive corpus search's three-second deadline, not a seven-second RDF
revision failure. That error is caught and the HUD continues without corpus
references. Expected corpus timeouts now log a concise warning; unexpected
errors still include their traceback.

Optional corpus reinforcement previously had no deadline after that search.
It now has a separate 350 ms budget (`corpus_maintenance_timeout_seconds`),
runs in a transaction, and rolls back its partial updates on cancellation.
A failed lookup skips reinforcement for that build. Successful reference hits
remain available even if maintenance times out. The lookup trace records
`maintenance_status` as well as `lookup_status`.

Corpus candidate SQL now materializes the union of FTS matches and suggested
section IDs before evaluating assignment and access predicates. There is no
candidate cap before access filtering. Deny rules, fanwork exclusion, ranking
and section passage coordinates are unchanged. This query shape permits an
independent FTS index scan; production plans are still needed to measure its
impact under load.

These changes do not bypass HUD readiness. `generation_ready:false` still
means the requested source needs its current message-cognition commit and
matching scene projection, or the source/runtime coordinates changed. A large
LIVE backlog can delay those even while topology reports `CAUGHT_UP`.
When investigating a missing SillyTavern HUD, preserve the status and JSON
response from `POST /instance/{id}/hud`, especially `generation_ready`,
`freshness`, and `frame.hud`. A corpus timeout alone does not establish which
of those conditions occurred. `wait_ms` controls readiness polling, not the
whole HTTP request deadline.

No additional migration is needed for this HUD corpus change. The continuation
worker still requires its existing migration and the seven-second RDF discovery
window is unchanged.
