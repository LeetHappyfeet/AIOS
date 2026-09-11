"""Compatibility imports for the pre-HUD epistemic retrieval layer.

Retrieval is cognition/state preparation, not HUD presentation. New code should
import these symbols from ``aios_app.epistemic.retrieval``. This module remains
only so older integrations do not break while the boundary migration settles.
"""

from aios_app.epistemic.retrieval import POLICIES, RetrievalPolicy, TopologyRetriever

__all__ = ["POLICIES", "RetrievalPolicy", "TopologyRetriever"]
