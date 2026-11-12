# SQL discovery starvation repair — 2026-11-08

## Motivation

After the first convergence repair, semantic vector upserts and semantic admission were
making progress, but topology quarantine still timed out at `next_batch=1`
and supervisor discovery kept timing out without creating queued jobs.
A `LIMIT 1` downstream of expensive eligibility predicates does not bound
how many source rows PostgreSQL must inspect.

## Changes

- `supervisor.py`: the four recurring discovery failures
  (`rdf_epistemic_project`, `derive_claim_topology`,
  `materialize_event_occurrences`, and
  `derive_character_acquisition_topology`) now scan a small, ordered page
  of source IDs and restrict the **unchanged** admission/eligibility query
  to those candidates. The existing foreground vs backlog quota logic of
  acquisition topology remains within the page. A page of ineligible
  candidates is advanced; a failed SQL query or failed job enqueue is retried.
  Page sizes reduce after SQL/lock timeouts and recover gradually.
- `semantic_index/eligibility.py`: `topology_nodes`,
  `event_memberships`, and `vectors` quarantine operate on small
  source/receipt pages before the expensive invalidity conditions.
  All withdrawal `OR` branches are constrained by the candidate page.
  Deletions still check admission and hold the existing mutation lock; Qdrant
  deletes must be acknowledged before receipt removal.
- `semantic_index/cli.py`: quarantine phases have independent adaptive
  maintenance batch sizes, so an expensive vectors scan no longer shrinks
  unrelated topology-node or event-membership batches.
- Diagnostics now include supervisor `scanned`, `eligible`, SQL elapsed,
  page size, and quarantine page scan timing.
- Tests exercise page advancement, cursor preservation after SQL failures,
  and the fully parenthesized topology-node delete predicate.

## Safety and limitations

In-memory scan cursors are *hints*, not durable progress receipts. All pages
wrap after exhaustion. A service restart begins a new sweep and cannot
mark unprocessed work as complete. No observation or proposition source
evidence is discarded by cursor advancement.

The supervisor's preexisting foreground priority is honored **within**
each scanned acquisition page. It is not a global foreground-first scan
across all historical rows; implementing durable priority-aware discovery
is a separate design pass.

A successful bounded scan does not prove SQL/Qdrant/Fuseki parity. Monitor
`sync DEGRADED`, SQL budget warnings, Qdrant receipt reconciliation,
and independent RDF projection backlog. This pass does not alter the SQL
budget defaults or add another migration.

## Verification

Run focused tests before launching AIOS:

```bash
python -m pytest -q tests/test_bounded_semantic_discovery.py \
  tests/test_quarantine_phase_budget.py \
  tests/test_semantic_topology_eligibility.py \
  tests/test_pipeline_liveness.py
```

Watch for `Bounded supervisor discovery ... scanned=... eligible=...` and
`Quarantine page phase=... scanned=...`. The page scans should make
progress even when all candidates in the first few pages are ineligible.
For persistent timeouts, inspect PostgreSQL `pg_stat_activity`,
`pg_locks`, and `EXPLAIN (ANALYZE, BUFFERS)` on a non-production replica
before raising statement timeouts.
