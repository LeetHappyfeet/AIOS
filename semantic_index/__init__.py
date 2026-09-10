"""AIOS semantic vector index.

Qdrant is a high-recall semantic candidate engine. PostgreSQL/RDF remain
authoritative for provenance, truth, world/branch membership and epistemic
visibility.
"""

from . import neighbor_classifier as _neighbor_classifier
from .relation_validator import validate_neighbor_relation as _validate_neighbor_relation
from .validation_adapter import validated_neighbor_classifier as _validated_neighbor_classifier

_original_neighbor_classifier = _neighbor_classifier.classify_neighbor_relations_once
_neighbor_classifier.classify_neighbor_pair = _validate_neighbor_relation


async def _run_validated_neighbor_classifier(db, cfg):
    return await _validated_neighbor_classifier(_original_neighbor_classifier, db, cfg)


_neighbor_classifier.classify_neighbor_relations_once = _run_validated_neighbor_classifier
