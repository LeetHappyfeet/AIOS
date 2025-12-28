from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from .fuseki import FusekiClient, FusekiError

logger = logging.getLogger("aios.rdf.world_writer")


# -------------------------------------------------
# Data model passed in by pipeline / janitor
# -------------------------------------------------

@dataclass
class WorldClaimWriteContext:
    dataset: str                 # e.g. "world"
    graph_iri: str               # named graph for the world
    claim_id: UUID

    world_key: str               # digimon | irl | liminal | etc

    subject: str                 # normalized IRI-safe identifier
    predicate: str               # normalized predicate
    object: Optional[str]        # optional object

    confidence: float

    source_document: Optional[str] = None
    citation: Optional[str] = None
    promoted_by: str = "worker"  # worker | janitor | manual


# -------------------------------------------------
# Helpers
# -------------------------------------------------

def _iri(prefix: str, value: str) -> str:
    safe = value.strip().replace(" ", "_")
    return f"urn:aios:{prefix}:{safe}"


def _lit(value: Optional[str]) -> str:
    if value is None:
        return '""'
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -------------------------------------------------
# Public API
# -------------------------------------------------

def write_world_claim(
    fuseki: FusekiClient,
    ctx: WorldClaimWriteContext,
) -> bool:
    """
    Writes a promoted world-level claim into the RDF world dataset.

    This function:
    - never extracts meaning
    - never changes confidence
    - never deletes anything
    - is safe to retry

    Returns True on success, False on failure.
    """

    world = _iri("world", ctx.world_key)
    subject = _iri("entity", ctx.subject)
    predicate = _iri("predicate", ctx.predicate)

    obj_triple = ""
    if ctx.object:
        obj_triple = f"aios:object {_iri('entity', ctx.object)} ;"

    sparql = f"""
PREFIX aios: <urn:aios:>

INSERT DATA {{
  GRAPH <{ctx.graph_iri}> {{

    <{world}> a aios:World .

    <{ctx.claim_id}> a aios:Claim ;
      aios:aboutWorld <{world}> ;
      aios:subject <{subject}> ;
      aios:predicate <{predicate}> ;
      {obj_triple}
      aios:confidence "{ctx.confidence}"^^xsd:float ;
      aios:promotedBy {_lit(ctx.promoted_by)} ;
      aios:sourceDocument {_lit(ctx.source_document)} ;
      aios:citation {_lit(ctx.citation)} .

  }}
}}
""".strip()

    try:
        fuseki.update(ctx.dataset, sparql)
        return True

    except FusekiError as e:
        logger.exception(
            "FusekiError while writing world claim %s", ctx.claim_id
        )
        return False

    except Exception:
        logger.exception(
            "Unexpected error while writing world claim %s", ctx.claim_id
        )
        return False
