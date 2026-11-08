# Partition-aware runner repair and benchmark

## Code changes

- Queue claims in `pipeline/jobs.py` now skip an already-running job's
  effective advisory-lock partition. `char:`, `source:`, and `world:`
  partitions are shared between SEMANTIC and RDF resource classes; other
  partitions are compared only within their resource class. This is an
  optimization, not the synchronization primitive: the existing advisory
  lock in `runner.py` is still mandatory because two processes may race
  during claim transactions.
- RDF jobs acquire `rdf_gate` before the PostgreSQL scope advisory lock.
  Cancellation/exception release the gate and the existing advisory lock.
  The runner logs RDF-gate waits >= 0.25 seconds.
- Existing lane reservations, foreground admission, and structural
  single-writer scheduling remain unchanged.
- No SQL migration or new partition-lease table is introduced.

## Benchmark procedure

1. On a stable instance, record the same 5–10-minute workload before and
   after deployment, preferably with comparable backlog and active scopes.
2. Compare `GLOBAL queued/running`, `in` and `out` rates, oldest job age,
   counts of `reason=partition advisory lock is busy`, median/p95 task
   runtimes, and number of different scopes completing work.
3. Look for `RDF gate wait` messages to distinguish RDF capacity pressure
   from advisory-lock contention.
4. Verify that no duplicate semantic projections or failed lease recovery
   events appear, that fresh LIVE work still proceeds, and that RDF changes
   continue publishing.

Example commands on your AIOS host, where `aios.log` contains one run:

```bash
grep -c 'reason=partition advisory lock is busy' aios.log
grep -c 'Job done' aios.log
grep 'GLOBAL ' aios.log | tail -20
grep 'RDF gate wait' aios.log | tail -20
```

The comparison is meaningful only for similarly sized work windows. Raw
deferral counts over different durations are not comparable; normalize them
per minute or per completed job.

## Caveats

The SQL claim filter reads `pipeline_job.partition_key`, which is filled at
enqueue and updated again at execution. Existing jobs with null/legacy
partition keys can still reach the advisory lock and defer. The filter
cannot eliminate races between separate runner processes, nor does it
make RDF/semantic processing parallel *within* one character scope.

If this first-pass benchmark shows remaining contention, inspect partition
key mismatches and SQL claim-query plans before introducing a durable
partition scheduler. A dedicated queue-status index can be considered
after measuring the actual plan; none was added in this pass.
