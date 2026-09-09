from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class ResourceClass(str, Enum):
    FAST_SQL = "FAST_SQL"
    NLP = "NLP"
    SEMANTIC = "SEMANTIC"
    VECTOR = "VECTOR"
    RDF = "RDF"
    RECONCILIATION = "RECONCILIATION"
    GLOBAL = "GLOBAL"


@dataclass(frozen=True)
class JobSpec:
    resource_class: ResourceClass
    partition_kind: str
    idempotent: bool
    isolate_blocking: bool = False


JOB_SPECS: Mapping[str, JobSpec] = {
    "discover_characters": JobSpec(ResourceClass.FAST_SQL, "character_id", True),
    "dag_to_document_section": JobSpec(ResourceClass.FAST_SQL, "node_id", True),
    "extract_claims": JobSpec(ResourceClass.NLP, "section_id", True, isolate_blocking=True),
    "resolve_claim_context": JobSpec(ResourceClass.SEMANTIC, "claim_scope", True),
    "normalize_proposition": JobSpec(ResourceClass.SEMANTIC, "claim_id", True),
    "project_character_knowledge": JobSpec(ResourceClass.SEMANTIC, "global", True),
    "derive_claim_topology": JobSpec(ResourceClass.SEMANTIC, "claim_scope", True),
    "derive_character_acquisition_topology": JobSpec(ResourceClass.SEMANTIC, "acquisition_scope", True),
    "derive_world_assertion_topology": JobSpec(ResourceClass.SEMANTIC, "assertion_scope", True),
    "resolve_generated_facts": JobSpec(ResourceClass.RECONCILIATION, "global", True),
    "rdf_epistemic_project": JobSpec(ResourceClass.RDF, "claim_scope", True, isolate_blocking=True),
    "rdf_liminal_promote": JobSpec(ResourceClass.RDF, "section_id", True, isolate_blocking=True),
    "rdf_liminal_classify": JobSpec(ResourceClass.RDF, "global", True, isolate_blocking=True),
    "project_world_topology": JobSpec(ResourceClass.RDF, "world_id", True, isolate_blocking=True),
    "assign_narratives": JobSpec(ResourceClass.GLOBAL, "global", True),
}


def job_spec(job_type: str) -> JobSpec:
    return JOB_SPECS.get(
        job_type,
        JobSpec(ResourceClass.GLOBAL, "global", False),
    )
