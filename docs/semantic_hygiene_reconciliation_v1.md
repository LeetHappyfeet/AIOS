# Semantic Hygiene Reconciliation V1 — operator-gated inverse lifecycle

**Default: SHADOW/PROPOSAL ONLY.** This is an additive post-baseline migration.
No automatic hygiene shadow result authorizes live withdrawal. The original
DAG, claim candidate, observation, proposition evidence, acquisition event,
semantic atom, and immutable shadow audit remain in PostgreSQL.

## Contract

A proposal is keyed by the exact claim ID, frame ID, proposition ID,
claim_semantic_integrity.revision_key, and validator_version, and must be
verifiable against a persisted repair_candidate or demote_candidate
hygiene-shadow source snapshot. A malformed shared semantic atom does NOT
license suppressing healthy independent claim occurrences.

- suppress_independent: source-grounded frame was materially malformed.
- demote_to_context: meaningful dependent frame is not independently assertable.

propose_semantic_hygiene_adjudication() verifies the exact original claim,
frame, proposition, raw text, and current integrity revision. It does not
change admissions. Applied decisions additionally require a named operator
and transaction-local aios.semantic_hygiene_apply_enabled=on; the CLI also
requires the environment flag AIOS_SEMANTIC_HYGIENE_APPLY_ENABLED=1.

The BEFORE trigger on semantic_evidence_admission runs after frame
interpretation admission, forces matching admissions to suppressed with zero
confidence, and prevents a later recomputation from restoring unchanged
rejected frames. Absence of support is NOT negative evidence.

Occurrence/proposition topology eligibility excludes applied CURRENT-revision
adjudications. Both Python claim topology paths select only exact eligible
(observation.claim_id, observation.proposition_id) pairs. The established
character_belief_reconciliation_dirty queue is populated for descendants and
all materialized legacy (instance_id,atom_id) coordinates; the serialized
belief materializer then removes unsupported belief states and corresponding
BELIEF_STATE topology nodes. Independent support survives.

The normal topology-quarantine stage removes ineligible derived topology and
deletes corresponding proposition and epistemic Qdrant points under the shared
vector mutation lock. Source evidence and historical records remain. The
topology delta outbox records SQL node/edge deletion. The implicated RDF scope
dirty version increments at apply and on later deletion/update of affected
topology nodes, preventing a projection/reconciliation race.

A new source/frame revision does not silently inherit an earlier adjudication.
For a new revision, review source evidence and supersede the earlier decision
under operator supervision; never create a blanket semantic-atom tombstone.

## Deploy and propose (proposal does NOT change knowledge)

From the beta package directory:

    python -m aios_app.migrate

Find the original Mia audit:

    SELECT audit_id, atom_id, disposition, reason_codes,
           jsonb_pretty(evidence_snapshot->'sources') AS sources
      FROM aios.semantic_hygiene_shadow_audit
     WHERE atom_id='16432edb-9565-468d-ba9d-75da96c572f0'::uuid
     ORDER BY created_at DESC;

Use that audit ID (the historical example was
624ebb8c-fd87-49af-9ec7-056a24e0e24d) to create an inert proposal:

    python -m aios_app.epistemic.hygiene_reconciliation \
      --propose-audit 624ebb8c-fd87-49af-9ec7-056a24e0e24d \
      --claim-id 211137e7-673e-46d2-895b-0b31007f3ae1 \
      --frame-id 2456f078-85a2-446d-b35f-f8bb945cb2e6 \
      --proposition-id c7f7a9bd-e7f1-4374-93d6-0c34ace5d6a2 \
      --action suppress_independent

Review the resulting source-bound proposal:

    SELECT a.adjudication_id,a.status,a.action,a.claim_id,a.frame_id,
           a.proposition_id,a.source_revision_key,
           ci.revision_key AS current_revision,a.source_text
      FROM aios.semantic_hygiene_adjudication a
      JOIN aios.claim_semantic_integrity ci ON ci.claim_id=a.claim_id
     ORDER BY a.proposed_at DESC LIMIT 10;

## Apply — NEVER automatic

Applying is a production-changing operation. Test first on a disposable
PostgreSQL/Fuseki/Qdrant copy. Do NOT apply against Experiment 7's live
character merely to generate experimental metrics.

    export AIOS_SEMANTIC_HYGIENE_APPLY_ENABLED=1
    python -m aios_app.epistemic.hygiene_reconciliation \
      --apply-id REVIEWED_ADJUDICATION_UUID --actor OPERATOR_NAME

Equivalent explicit psql transaction:

    BEGIN;
    SET LOCAL aios.semantic_hygiene_apply_enabled = on;
    SELECT aios.apply_semantic_hygiene_adjudication(
      'REVIEWED_ADJUDICATION_UUID'::uuid,'OPERATOR_NAME');
    COMMIT;

Apply updates effective admissions, queues descendant reconciliation, marks
impacted RDF scopes dirty, and records one append-only event. It deliberately
does not synchronously delete all ten belief states. Let the existing
reconciliation stage drain, then topology quarantine and RDF catch up.
Admission recomputation for the unchanged revision remains suppressed.

## Verify Mia

    SELECT status,reason,confidence,count(*)
      FROM aios.semantic_evidence_admission
     WHERE acquisition_id IN (
       SELECT acquisition_id FROM aios.knowledge_acquisition_event
       WHERE claim_id='211137e7-673e-46d2-895b-0b31007f3ae1'::uuid
         AND proposition_id='c7f7a9bd-e7f1-4374-93d6-0c34ace5d6a2'::uuid)
     GROUP BY status,reason,confidence;

    SELECT count(*) AS dirty_instances
      FROM aios.character_belief_reconciliation_dirty
     WHERE atom_id='16432edb-9565-468d-ba9d-75da96c572f0'::uuid;

    SELECT count(*) AS surviving_beliefs
      FROM aios.character_belief_state
     WHERE atom_id='16432edb-9565-468d-ba9d-75da96c572f0'::uuid;

    SELECT node_type,count(*)
      FROM aios.semantic_topology_node
     WHERE proposition_id='c7f7a9bd-e7f1-4374-93d6-0c34ace5d6a2'::uuid
     GROUP BY node_type ORDER BY node_type;

    SELECT scope_key,dirty_version,projected_version,status
      FROM aios.semantic_scope_projection_state
     WHERE scope_key='char:Renamon';

No negative belief is introduced. Healthy independently supported occurrences
survive. Repeating the same apply is idempotent; new frame revisions must be
adjudicated separately.

## Tests

    pytest -q tests/test_semantic_hygiene_reconciliation.py
    # Against an empty disposable PostgreSQL 16 database ONLY:
    AIOS_DB_DSN=postgresql://... python tests/ci_hygiene_reconciliation_smoke.py

CI: hygiene-shadow and hygiene-reconciliation-smoke.
