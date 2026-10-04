# AIOS Experiment 8 — pre-reset release gate (P7/P8)

The existing `aios_baseline.sql` and immutable post-baseline SQL history are
the canonical development schema. This pass **does not rewrite, silently
squash, or re-hash** those files. It consolidates their installation into ONE
ordered, full-chain migrator and full-chain CI acceptance path. Retain an
existing canonical ledger or intentionally rebuild a disposable DB; prototype
ledgers are not auto-upgraded.

## Authority and provenance (P7)

`component_versions()` is an **installed code** inventory, NOT an assertion
about which PostgreSQL policy controls live memory. The authoritative view is
`capture_runtime_manifest(db)` on the same database connection that writes
a message-cognition commit or participation comparison.

Manifest version: `aios-effective-runtime-manifest-v2`.

The persisted manifest separates:
- `installed_components`: loaded worker code constants
- `effective_authority`: inspected SQL wrapper, authority materializer,
  family-policy materializer, strict V4 source gate and required migration
  receipts; missing/inconsistent components resolve to `unverified`
- `shadow_comparators`: participation V1/V2/V3/V4 execution modes, never
  mistaken for live memory admission
- `receipt_versions`: the actual message interpreter, Integrity receipt or
  specific comparison version/mode for the persisted operation
- `migration_receipts`: immutable migration names and SHA-256 digests
- `sql_fingerprints`: hashes of active PostgreSQL function definitions

The introspection endpoint is read-only:

    GET /agent/runtime/versions

`python -m aios_app.db_check` also fails startup readiness if the effective
belief executor or V4 source contract is unverified. Installed Python
constants alone cannot satisfy the check.

## Canonical migration execution (P8)

`python -m aios_app.migrate` now:
1. Checks the presence of critical reference-data/integration migration files.
2. Validates SQL outer transaction wrappers BEFORE touching the database.
3. Takes one PostgreSQL advisory lock for serialized initialization.
4. Applies the immutable baseline and its receipt atomically.
5. Validates the ledger is a checksum-matching PREFIX of the canonical chain.
6. Applies each migration SQL body and ledger INSERT inside ONE transaction.
7. Fails on unknown/prototype ledger entries or changed applied SQL.
8. Can be rerun without inventing extra receipts or modifying existing DDL.

Do not modify a previously applied migration to repair a failure. Add a
reviewed additive migration, re-run the complete fresh-chain test, and then
proceed with the controlled reset.

### Required CI before wiping AIOS1

The `canonical-cold-start` GitHub Actions job runs production migration code
against completely empty PostgreSQL 16, runs the runtime preflight, checks all
migration digests and effective authority, runs migration a second time for
idempotence and injects one failing synthetic final migration. The synthetic
probe asserts that neither its table nor its ledger receipt survives.

The smaller selected-migration integrity and hygiene smokes remain independent
diagnostics, **not** substitutes for the full canonical-chain job. P4–P6
regression tests remain required for the Alex testimony, goal completion and
participation heartbeat defects.

Do not start Experiment 8 until the full CI job and source-fidelity/goal
regressions pass on the exact intended branch SHA.

## Operator reset sequence (NOT performed by this patch)

1. Stop AIOS launch and optional background workers before making snapshots.
2. Export the Experiment 7 JSON, old HUD frames, source identifiers,
   current migration/version manifest, and a PostgreSQL dump outside the data
   stores being wiped.
3. Verify the actual development DSN points to `aiosdb_fresh_test`, not a
   production database. Inspect Qdrant's bind mount and Fuseki dataset names
   individually before deletion. Do not assume restarting a container deletes
   its persisted volumes.
4. Deliberately recreate ONLY the AIOS development PostgreSQL database, clear
   ONLY its matching Qdrant collections/persistent storage and ONLY its
   development Fuseki datasets. Preserve source documents for reproducibility.
5. Run `python -m aios_app.migrate`, then `python -m aios_app.db_check`.
6. Before new ingestion, inspect `GET /agent/runtime/versions`; require
   effective belief authority `character-belief-v4-authority-family` and
   source contract `integrity-contract-v2-source-coordinate`.
7. Enable `AIOS_PARTICIPATION_SHADOW_ENABLED=1` ONLY for a deliberately paired
   comparison. Check worker heartbeat before enrolling an experiment.
8. Run deterministic claim fixtures first, then the Renamon continuity run,
   then the 100-claim V3/V4 paired evaluation. Never use old zero-row Experiment
   7 comparison as a performance outcome.

There are no data-wiping operations in this patch. The actual AIOS1 reset is
a separate, operator-authorized step.
