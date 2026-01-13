# aios_app/rdf/world_asserted.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from .fuseki import FusekiClient, FusekiError

logger = logging.getLogger("aios.rdf.world_writer")


# -------------------------------------------------
# Data model passed in by promotion policy / janitor
# -------------------------------------------------

@dataclass
class WorldClaimWriteContext:
    dataset: str                 # e.g. "world"
    graph_iri: str               # named graph for the asserted world
    claim_id: UUID

    world_key: str               # e.g. "irl_main", "world_A"

    subject: str                 # normalized IRI-safe identifier
    predicate: str               # normalized predicate
    object: Optional[str]        # optional object

    confidence: float

    # ---- Provenance / justification ----
    timeline_id: Optional[UUID] = None
    dag_node_id: Optional[UUID] = None
    promotion_rule: Optional[str] = None
    promotion_version: Optional[str] = None

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
    Writes a promoted claim into an asserted world graph.

    This function:
    - records provenance (DAG / timeline / policy)
    - never infers meaning
    - never mutates confidence
    - never deletes anything
    - is safe to retry
    """

    world_iri = _iri("world", ctx.world_key)
    subject_iri = _iri("entity", ctx.subject)
    predicate_iri = _iri("predicate", ctx.predicate)

    object_triple = ""
    if ctx.object:
        object_triple = f"aios:object {_iri('entity', ctx.object)} ;"

    # -----------------------------
    # Provenance triples (optional)
    # -----------------------------

    prov_triples = ""

    if ctx.timeline_id:
        prov_triples += (
            f"      prov:wasDerivedFrom <urn:aios:timeline:{ctx.timeline_id}> ;\n"
        )

    if ctx.dag_node_id:
        prov_triples += (
            f"      prov:wasDerivedFrom <urn:aios:dag:node:{ctx.dag_node_id}> ;\n"
        )

    if ctx.promotion_rule:
        prov_triples += (
            f"      prov:wasGeneratedBy <urn:aios:promotion:rule:{ctx.promotion_rule}> ;\n"
        )

    if ctx.promotion_version:
        prov_triples += (
            f'      aios:promotionVersion "{ctx.promotion_version}" ;\n'
        )

    sparql = f"""
PREFIX aios: <urn:aios:>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>

INSERT DATA {{
  GRAPH <{ctx.graph_iri}> {{

    <{world_iri}> a aios:World .

    <urn:aios:world:claim:{ctx.claim_id}> a aios:Claim ;
      aios:aboutWorld <{world_iri}> ;
      aios:subject <{subject_iri}> ;
      aios:predicate <{predicate_iri}> ;
      {object_triple}
      aios:confidence "{ctx.confidence}"^^xsd:float ;
      aios:promotedBy {_lit(ctx.promoted_by)} ;
      aios:sourceDocument {_lit(ctx.source_document)} ;
      aios:citation {_lit(ctx.citation)} ;
{prov_triples.rstrip(" ;\n")}
      .

  }}
}}
""".strip()

    try:
        fuseki.update(ctx.dataset, sparql)
        return True

    except FusekiError:
        logger.exception(
            "FusekiError while writing asserted world claim %s",
            ctx.claim_id,
        )
        return False

    except Exception:
        logger.exception(
            "Unexpected error while writing asserted world claim %s",
            ctx.claim_id,
        )
        return False
