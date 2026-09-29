"""Compatibility imports for the pre-HUD epistemic retrieval layer.

Retrieval is cognition/state preparation, not HUD presentation. New code should
import these symbols from ``aios_app.epistemic.halo_retrieval``. This module
remains only so older integrations do not break while the boundary migration
settles.
"""

from aios_app.epistemic.halo_retrieval import (
    POLICIES,
    RetrievalPolicy,
    TopologyRetriever,
)
from aios_app.epistemic.retrieval import MAX_FOCUS_TERMS, _RETRIEVAL_SQL, _focus_terms

__all__ = [
    "MAX_FOCUS_TERMS",
    "POLICIES",
    "RetrievalPolicy",
    "TopologyRetriever",
    "_RETRIEVAL_SQL",
    "_focus_terms",
]
