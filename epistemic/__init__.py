"""Epistemic normalization, narratives, knowledge projection, and gap-fill control."""

from .normalizer import normalize_claim_once
from .narratives import assign_narratives_once
from .knowledge import project_knowledge_acquisitions_once, record_acquisition
from .generated import create_generated_fact, resolve_generated_facts_once

# Claim topology is split from the legacy topology module so semantic ownership
# can evolve independently from world/assertion/acquisition projection. Keep the
# legacy module entry points wired for existing callers until imports are moved
# directly to topology_claims.
from . import topology as _topology
from .topology_claims import (
    choose_observation_scope as _choose_observation_scope,
    derive_claim_topology as _derive_claim_topology,
)

_topology.choose_observation_scope = _choose_observation_scope
_topology.derive_claim_topology = _derive_claim_topology

__all__ = [
    "normalize_claim_once",
    "assign_narratives_once",
    "project_knowledge_acquisitions_once",
    "record_acquisition",
    "create_generated_fact",
    "resolve_generated_facts_once",
]
