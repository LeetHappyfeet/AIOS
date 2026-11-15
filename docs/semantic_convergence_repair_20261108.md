# Semantic convergence and SQL discovery repair — 2026-11-08

## Incident

The pipeline reported `GLOBAL CAUGHT_UP 0q/0r` while independent semantic-index,
topology and supervisor discovery stages repeatedly hit PostgreSQL statement
budgets. `topology-quarantine/vectors` also reached its cooldown threshold.
The pipeline queue is **not** a complete SQL/Qdrant/Fuseki parity indicator.

## Repairs

- Phased topology quarantine now resolves `topology-quarantine/vectors` and
  similar stage names to their adaptive background batch budget. The next
  attempt uses the reduced batch size. Phase cooldowns and current adaptive
  sizes are surfaced in topology telemetry.
- Proposition and epistemic vector discovery use bounded, rotating keyset
  pages of receipt-missing source rows. Expensive eligibility and contextual
  lookups occur *after* the page has been selected. A long run of ineligible
  records no longer pins discovery on the newest page. Missing receipts remain
  pending; cursors are scanning hints, **not** success acknowledgements.
- Semantic-admission discovery similarly pages through exact-novel decisions
  before examining topology readiness and vector receipts.
- SQL timeout failures reduce proposition, epistemic and admission batch sizes,
  with gradual recovery after successful cycles.
- Vector quarantine now drives candidate discovery from typed vector receipts
  rather than an `OR` join containing an unanchored `LIKE` suffix.
- New indexes cover the keyset scan order and receipt-driven cleanup.
- Launcher displays `sync DEGRADED` independently of `GLOBAL CAUGHT_UP`
  and prints failed semantic stages and pending admission count. Semantic
  telemetry reports failure streaks and adaptive sizes. It explicitly marks
  cross-store convergence as **unverified**, rather than inferring parity
  from an empty queue.

## Non-goals / safety

No source observations, claim candidates, propositions or character beliefs
are deleted by this repair. Qdrant writes and deletes keep the existing
receipt-after-acknowledgement behavior and shared mutation lock.

The scan cursors are in-memory and reset at service restart. This is safe
because PostgreSQL receipts remain authoritative; however a restart starts
the scan from newest again. The change is bounded progressive discovery,
not a durable pending-work queue. Full SQL/Qdrant/Fuseki reconciliation still
requires a separate cross-store audit. Supervisor discovery SQL plans are
not silently rewritten or timeout budgets increased.

## Operator validation

1. Apply current migrations, then restart the semantic index, topology and
   launcher services. On large PostgreSQL tables, apply new indexes during a
   maintenance window because normal index creation takes write locks.
2. Watch for `semantic failed=...`, `sync DEGRADED`, and
   `adaptive_batch_sizes` in emitted `AIOS_TELEMETRY`. Success is *not*
   proven by `GLOBAL CAUGHT_UP` alone.
3. If supervisor discovery continues hitting its 5-second budget, capture
   `EXPLAIN (ANALYZE, BUFFERS)` for the specific stage's eligibility query
   against a safe replica or maintenance window. Also inspect
   `pg_stat_activity` and `pg_locks` to distinguish SQL-plan cost from
   lock waiting. The incident logs do not prove which cause dominates.
4. Verify Qdrant point counts/point IDs against the SQL
   `semantic_vector_index_state` receipt rows, and compare pending
   admission/topology/RDF projections separately. Do not assume a receipt
   implies Qdrant is currently healthy after external data loss.

## Tests

Added regression coverage for phased adaptive batching, independent sync
health rendering, rotating discovery cursors, timeout-safe cursor state and
admission readiness. A live PostgreSQL/Qdrant/Fuseki integration run is still
required to prove convergence and measure query plans on the affected dataset.
