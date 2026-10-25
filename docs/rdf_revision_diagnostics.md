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
