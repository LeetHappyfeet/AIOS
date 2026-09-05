"""Epistemic normalization, narratives, knowledge projection, and gap-fill control."""

from .normalizer import normalize_claim_once
from .narratives import assign_narratives_once
from .knowledge import project_knowledge_acquisitions_once, record_acquisition
from .generated import create_generated_fact, resolve_generated_facts_once

__all__ = [
    "normalize_claim_once",
    "assign_narratives_once",
    "project_knowledge_acquisitions_once",
    "record_acquisition",
    "create_generated_fact",
    "resolve_generated_facts_once",
]
