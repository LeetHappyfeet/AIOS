# Cognitive episode recovery

The `20261014_01_cognitive_episode_recovery.sql` migration adds durable host
cognition episodes and replayable cleanup receipts. Apply migrations before
starting the updated API, supervisor, or pipeline workers:

```bash
python -m aios_app.migrate
```

Stop the old processes before migration and restart the updated processes after
it completes. This patch changes SQL contracts; mixed old/new workers are not a
supported deployment mode. No memory-store reset is required.

## Completion rules

An episode represents one character instance and one source DAG node. Admission
records the attention transaction and reserved goal-maintenance operation in one
transaction. A retry reuses an active or successfully completed admission.
Failed episodes can be readmitted through the existing bounded host-delta retry
policy.

The episode is processed only after the attention transaction is consumed and
all its operations have succeeded with reconciled terminal receipts. Choosing
“wait” completes attention without an operation. A reserved goal review still
has to finish. Successful research completes its bounded cognition work; it
does not assert that a goal is satisfied or that a knowledge requirement is met.

Acknowledgement checks the source timeline and event ordering. An older receipt
cannot move the cursor backward or clear a newer pending delta.

## Recovery

The supervisor's independent transaction scheduler runs a bounded recovery
sweep before offering new inference work. The sweep expires pending, running,
and proposed decisions at their deadlines, suppresses their abandoned offers,
and terminates linked operations. It also repairs queued operations without
jobs, reconciles terminal cleanup receipts, and checks active episodes.

A running operation older than ten minutes is abandoned only when no matching
pipeline job retains a live worker lease. Waiting operations with a failed
decision are reconciled, and waiting operations without a decision or a queued
or running inference job are recovered after ten minutes. Healthy pipeline
workers retain ownership; recovery does not rerun an expired decision's model
response. A late response cannot materialize a decision already expired by the
sweep.

Cleanup runs atomically and writes `reconciled_at` only after its side effects
succeed. An interruption leaves a receipt the next sweep can replay. Historic
consumed transactions and terminal operations are marked as already reconciled
during migration to avoid replaying old goal effects. Historic failed decision
transactions remain eligible for cleanup, including linked nonterminal
operations and offered opportunities. Ambiguous historic successful cleanup
cannot be reconstructed automatically from old receipts.

Thread-crossing evidence is stored as a JSON array. Migration unwraps the known
legacy JSON-string encoding when it contains a valid array; malformed legacy
values remain available for inspection.

## Verification

The isolated PostgreSQL engine tests use PGlite and require no application DB or
inference endpoint. The `Cognitive episode recovery` CI workflow installs its
pinned engine and executes migration, admission rollback, deduplication, episode
barrier, expiry, and cursor-ordering regressions. Unit tests also verify that
nested services share one transaction connection while child asyncio tasks
acquire separate sessions rather than sharing asyncpg concurrently.
