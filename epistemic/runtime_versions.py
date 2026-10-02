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
    }
