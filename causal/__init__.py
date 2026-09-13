"""Branch-coherent deterministic state and causal admission for AIOS.

The causal package is deliberately below semantic/epistemic projection.  It
owns admissibility of objective state transitions; it does not decide what a
character believes and it never writes character knowledge.
"""

from .kernel import CausalIntegrityKernel, CausalConflict, CausalRejected
from .types import AdmissionDecision, CausalCandidate, CausalEvaluation

__all__ = [
    "AdmissionDecision",
    "CausalCandidate",
    "CausalConflict",
    "CausalEvaluation",
    "CausalIntegrityKernel",
    "CausalRejected",
]
