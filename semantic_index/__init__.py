"""AIOS semantic vector index.

Qdrant is a high-recall semantic candidate engine. PostgreSQL/RDF remain
authoritative for provenance, truth, world/branch membership and epistemic
visibility.
"""

from . import neighbor_classifier as _neighbor_classifier
from . import reconciliation as _reconciliation
from .relation_validator import validate_neighbor_relation as _validate_neighbor_relation
from .validation_adapter import (
    record_new_relation_decisions as _record_new_relation_decisions,
    validated_neighbor_classifier as _validated_neighbor_classifier,
)

_original_neighbor_classifier = _neighbor_classifier.classify_neighbor_relations_once
_original_neighbor_reconciliation = _reconciliation.reconcile_neighbor_relations_once
_neighbor_classifier.NEIGHBOR_CLASSIFIER_VERSION = "semantic-neighbor-classifier-v4-event-identity"
_neighbor_classifier.classify_neighbor_pair = _validate_neighbor_relation


async def _run_validated_neighbor_classifier(db, cfg):
    return await _validated_neighbor_classifier(_original_neighbor_classifier, db, cfg)


async def _run_validated_neighbor_reconciliation(db, fuseki, cfg):
    # Re-evaluate stale/missing pair decisions from the durable classifier
    # receipts before any relation is allowed to create accepted topology.
    await _record_new_relation_decisions(db, limit=max(100, cfg.batch_size * 4))

    # The reconciliation implementation consumes raw classifier receipts. Keep
    # that advisory surface intact for clustering/debugging, but do not let the
    # promotion path run while any candidate relation in this classifier scope
    # still has a stale or missing validation decision. A later pass resumes as
    # soon as validation reaches its fixpoint.
    pending_validation = await db.fetchrow(
        """
        SELECT 1
        FROM aios.semantic_neighbor_relation r
        WHERE r.embedding_version=$1
          AND r.classifier_version=$2
          AND r.status='candidate'
          AND NOT EXISTS (
              SELECT 1
              FROM aios.semantic_validation_decision d
              WHERE d.decision_type IN ('proposition_relation','event_identity')
                AND d.decision_key=(r.proposition_id::text || ':' || r.neighbor_proposition_id::text)
                AND d.selected_value=r.relation
                AND d.status <> 'stale'
          )
        LIMIT 1
        """,
        cfg.embedding_version,
        _neighbor_classifier.NEIGHBOR_CLASSIFIER_VERSION,
    )
    if pending_validation:
        return 0

    return await _original_neighbor_reconciliation(db, fuseki, cfg)


_neighbor_classifier.classify_neighbor_relations_once = _run_validated_neighbor_classifier
_reconciliation.reconcile_neighbor_relations_once = _run_validated_neighbor_reconciliation
