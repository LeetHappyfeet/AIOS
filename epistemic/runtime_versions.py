"""Versions loaded by the running worker, not assumed from Git branch metadata."""
from __future__ import annotations


def component_versions() -> dict[str, str]:
    from aios_app.epistemic import (
        semantic_frames, context_resolver, semantic_integrity, normalizer,
        message_cognition,
    )
    from aios_app.agent import participation
    return {
        "semantic_frames": semantic_frames.DECOMPOSER_VERSION,
        "context_resolver": context_resolver.RESOLVER_VERSION,
        "semantic_integrity": semantic_integrity.INTEGRITY_VERSION,
        "normalizer": normalizer.NORMALIZER_VERSION,
        "message_cognition": message_cognition.INTERPRETER_VERSION,
        "participation_primary": participation.POLICY_VERSION,
        "participation_v2": participation.COMPARISON_VERSION,
        "participation_v3": participation.V3_VERSION,
        "participation_v4": participation.V4_VERSION,
        # Installed comparison modules are not automatically authoritative.
        "participation_primary_execution": "shadow-v1-ledger-only",
        "participation_v2_execution": "shadow-comparison-only",
        "participation_v3_execution": "shadow-comparison-only",
        "participation_v4_execution": "shadow-comparison-only",
        "participation_live_admission": "none",
        "belief_materializer": "character-belief-v4-authority-family",
        "belief_authority_base": "character-belief-v3-authority-lineage",
        "belief_family_policy": "semantic-policy-v1",
        "source_integrity_contract": "integrity-contract-v1",
    }
