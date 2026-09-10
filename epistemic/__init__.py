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
from . import topology_claims as _topology_claims
from .ownership import resolve_semantic_ownership as _resolve_semantic_ownership
from .ownership_verifier import verify_semantic_ownership as _verify_semantic_ownership


async def _resolve_and_verify_semantic_ownership(db, row):
    proposed = await _resolve_semantic_ownership(db, row)
    return await _verify_semantic_ownership(db, row, proposed)


# The claim projector resolves first and then subjects inferred ownership to the
# adversarial matrix. Explicit ownership remains authoritative but still records
# verifier evidence in topology metadata.
_topology_claims.resolve_semantic_ownership = _resolve_and_verify_semantic_ownership
_topology.choose_observation_scope = _topology_claims.choose_observation_scope
_topology.derive_claim_topology = _topology_claims.derive_claim_topology

__all__ = [
    "normalize_claim_once",
    "assign_narratives_once",
    "project_knowledge_acquisitions_once",
    "record_acquisition",
    "create_generated_fact",
    "resolve_generated_facts_once",
]
