from __future__ import annotations

"""Public semantic-first context resolver facade."""

from aios_app.epistemic.context_resolver_legacy import (
    ClaimContext,
    DATASET,
    ENTITY_KINDS,
    LIMINAL_GRAPH,
    PIVOT_KINDS,
    PREDICATE_FAMILIES,
    PREDICATE_GROUPS,
    RDF_RECEIPT_PREDICATE,
    classify_claim_kind,
    classify_entity_kind,
    classify_predicate_family,
    infer_acquisition_mode,
    is_semantic_pivot,
    resolve_ingest_viewpoint,
)
from aios_app.epistemic.context_resolver_v4 import RESOLVER_VERSION, resolve_claim_context

__all__ = [
    "ClaimContext",
    "DATASET",
    "ENTITY_KINDS",
    "LIMINAL_GRAPH",
    "PIVOT_KINDS",
    "PREDICATE_FAMILIES",
    "PREDICATE_GROUPS",
    "RDF_RECEIPT_PREDICATE",
    "RESOLVER_VERSION",
    "classify_claim_kind",
    "classify_entity_kind",
    "classify_predicate_family",
    "infer_acquisition_mode",
    "is_semantic_pivot",
    "resolve_claim_context",
    "resolve_ingest_viewpoint",
]
