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
_neighbor_classifier.classify_neighbor_pair = _validate_neighbor_relation


async def _run_validated_neighbor_classifier(db, cfg):
    return await _validated_neighbor_classifier(_original_neighbor_classifier, db, cfg)


async def _run_validated_neighbor_reconciliation(db, fuseki, cfg):
    # Refresh stale, missing, obsolete, or relation-mismatched validation
    # decisions before promotion. Reconciliation itself gates each relation on
    # its own current verified decision, so unrelated pending work cannot stall
    # the entire semantic promotion loop.
    await _record_new_relation_decisions(db, limit=max(100, cfg.batch_size * 4))
    return await _original_neighbor_reconciliation(db, fuseki, cfg)

_neighbor_classifier.classify_neighbor_relations_once = _run_validated_neighbor_classifier
_reconciliation.reconcile_neighbor_relations_once = _run_validated_neighbor_reconciliation
