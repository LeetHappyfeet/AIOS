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
No database migration is needed for these additions; the previous continuation
migration remains required.

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

The seven-second change deliberately preserves query predicates, result limits,
fingerprint inputs, source authorization and timeline boundaries. It is a wider
measurement window, not a claim that the underlying queries are optimized.
Continuation still checks dependencies sequentially in the supervisor while
holding the plan's goal/runtime locks; several slow dependencies can delay other
work. Query plans are needed before deciding whether to move fingerprinting to
smaller indexed coordinates or a separate background job.

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

The next query optimization to evaluate is to materialize the topic/owner/authority
candidate set before invoking expensive source eligibility, while retaining every
admission and branch predicate on the eventual results. Actual performance and
identical authorization results need comparison before deploying that rewrite.

The launch log also exposed a separate inquiry fallback defect: RDF failure or
an empty result led to character knowledge retrieval with `scorer=None`, which
then called `scorer.score()`. Inquiry now supplies a query-focused scorer, and
the optional-scorer knowledge API initializes its default when omitted. This fixes
the AttributeError without changing source-access SQL or the seven-second window.
