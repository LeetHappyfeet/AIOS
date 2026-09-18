"""Compatibility exports for cognition-owned relevance.

New code must import from aios_app.epistemic.relevance. This module remains
temporarily so plugins and tests using the historical HUD import do not break.
"""

from aios_app.epistemic.relevance import (
    CognitiveRelevanceBreakdown as RelevanceBreakdown,
    CognitiveRelevanceScorer as HUDRelevanceScorer,
)

__all__ = ["HUDRelevanceScorer", "RelevanceBreakdown"]
