# RDF-assisted semantic hygiene: shadow pass v1

## Purpose and authority

The extractor's false-valid outcomes include incomplete coordination (mia and /
keep / missing object), detached relative clauses (but a script / have / the
right apartment), truncated coordinated objects (porcelain figurines and) and
resultative motion errors (her tail / go / missing state from "gone still").

This pass uses PostgreSQL source evidence and actual Fuseki /char atom triples
to discover potentially unsound semantic identities. **No pruning happens in
v1.** RDF lag, graph isolation and low connectivity do not prove invalidity.
A disposition is an audit suggestion, never admission or deletion authority.

## Deployment

Apply migrations/current/20261003_07_semantic_hygiene_shadow.sql through the
normal python -m aios_app.migrate process. Do NOT modify the immutable baseline.

Set AIOS_SEMANTIC_HYGIENE_SHADOW_ENABLED=1 on the runner to opt in; default
is OFF. The scheduler admits at most one shadow batch per minute. Each batch
scans at most 16 atoms, performs one read-only Fuseki SELECT and writes only
the new audit and cursor tables. The worker is BACKGROUND/RDF priority 290,
behind scope projection (200) and legacy compaction (250).

The V3-only population is processed first; then a suspicious V4 belief sample.
Populations are based on currently valid materialized evidence. Original claim
integrity receipts are never overwritten. Completed cursors stop scheduling
until an explicit reset or a future versioned experiment.

## Inspection SQL (read only)

    SELECT * FROM aios.semantic_hygiene_shadow_cursor
    ORDER BY population;

    SELECT population, disposition, COUNT(*) AS audits,
           COUNT(DISTINCT atom_id) AS atoms
    FROM aios.semantic_hygiene_shadow_audit
    WHERE policy_version='semantic-hygiene-shadow-v1'
    GROUP BY population, disposition
    ORDER BY population, disposition;

    SELECT atom_id, disposition, reason_codes, source_claim_ids,
           source_revision_keys, rdf_graphs, impact,
           evidence_snapshot->'atom' AS atom
    FROM aios.semantic_hygiene_shadow_audit
    ORDER BY created_at DESC LIMIT 30;

To deliberately re-scan after saving the previous audit snapshot:

    UPDATE aios.semantic_hygiene_shadow_cursor
    SET last_atom_id=NULL, completed_at=NULL, updated_at=now()
    WHERE policy_version='semantic-hygiene-shadow-v1'
      AND population='v3_only';

The immutable audit is deduplicated by (atom_id, policy_version,
source_signature) including source/frame revision and RDF representation.
Unchanged re-scans do not insert duplicate audits.

## Signals and limitations

- Subject boundaries, missing expected arguments, incomplete coordinated
  objects, dependent clauses, and resultative "gone still" are diagnostic
  signals, never globally sufficient rejection criteria.
- A single batched read-only SPARQL query checks each atom's published subject,
  predicate and object against SQL. Fuseki errors fail the batch BEFORE cursor
  advancement. RDF absence for an active belief means needs_review, not prune.
- The audit includes proposition/evidence counts, affected belief states and
  instances, directly linked topology nodes, incident edges, linked anchors,
  and potentially affected scopes. This is an impact estimate, NOT a safe
  prediction of which entities may be physically deleted.
- More than 128 provenance rows per atom is recorded as truncated, requiring
  review. Materialized evidence without a valid claim integrity receipt is
  flagged for review.
- Dispositions: retain, repair_candidate, demote_candidate, needs_review.
  The schema reserves retraction_candidate for a future independently approved
  lifecycle; v1 never emits it automatically.

## Required inverse lifecycle, deliberately NOT executed by this patch

1. Verify the exact originating source occurrence and frame, not merely
   the shared atom identity.
2. Persist adjudication keyed to source/frame revision and hygiene policy
   version; prevent oscillating repair loops.
3. Retract only authorized invalid evidence transactionally in PostgreSQL,
   preserving independent V4 support for any shared V3+V4 atom.
4. Reconcile character-instance beliefs, dirty the existing RDF belief outbox,
   derive topology contraction including incident edges and cross-scope anchors
   before removing nodes, and record the durable semantic_rdf_change outbox.
5. Let the current RDF delta/full rebuild projector publish reduced graphs.
   Verify parity, replay after failure and detect orphan relationships.

The RDF delta projector already supports deleting projected topology resources.
The upstream inverse evidence -> belief -> topology lifecycle is NOT complete.
Direct Fuseki DELETE WHERE is not a viable shortcut: SQL derivation may recreate
the same branches.

## Regression tests

    pytest -q tests/test_semantic_hygiene_shadow.py
    pytest -q tests/test_topology_projection_batching.py tests/test_rdf_scope_scheduler_regression.py
