from __future__ import annotations

import logging
from uuid import UUID
from typing import Iterable

from ..db import Database
from .fuseki import FusekiClient

logger = logging.getLogger("aios.rdf.world_liminal")

DATASET = "world"
GRAPH_IRI = "urn:aios:world:liminal"


# -------------------------------------------------
# Public entrypoint (BATCHED)
# -------------------------------------------------

async def promote_liminal_claims(
    db: Database,
    fuseki: FusekiClient,
    *,
    batch_size: int = 100,
) -> int:
    """
    Promote claim_candidate → RDF /world/liminal

    Semantics:
    - Liminal is NOT a world
    - Claims here are observational only
    - No truth, belief, or world membership is asserted
    """

    rows = await _fetch_claims(db, batch_size)

    if not rows:
        return 0

    try:
        _write_claims_rdf_batch(fuseki, rows)
    except Exception:
        logger.exception("Failed batch promotion to /world/liminal")
        return 0

    for row in rows:
        await _log_promotion(db, row["claim_id"])

    logger.info("Promoted %d claims into /world/liminal", len(rows))
    return len(rows)


# -------------------------------------------------
# SQL
# -------------------------------------------------

async def _fetch_claims(db: Database, limit: int) -> Iterable[dict]:
    return await db.fetch(
        """
        SELECT
            cc.claim_id,
            cc.subject,
            cc.predicate,
            cc.object,
            cc.raw_text,
            cc.confidence,
            cc.extraction_rule,
            cc.extraction_ver,
            cc.created_at,
            cp.document_id
        FROM aios.claim_candidate cc
        LEFT JOIN aios.claim_provenance cp
          ON cp.claim_id = cc.claim_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.rdf_promotion_log rpl
            WHERE rpl.claim_id = cc.claim_id
              AND rpl.rdf_dataset = 'world'
              AND rpl.rdf_graph = 'urn:aios:world:liminal'
        )
        ORDER BY cc.created_at
        LIMIT $1
        """,
        limit,
    )


async def _log_promotion(db: Database, claim_id: UUID) -> None:
    await db.execute(
        """
        INSERT INTO aios.rdf_promotion_log (
            claim_id,
            rdf_dataset,
            rdf_graph,
            rdf_subject,
            rdf_predicate,
            rdf_object,
            promoted_by,
            promoted_at
        )
        VALUES (
            $1::uuid,
            'world',
            'urn:aios:world:liminal',
            'urn:aios:world:claim:' || $1::text,
            'rdf:type',
            'world:Claim',
            'world_liminal_writer',
            now()
        )
        ON CONFLICT DO NOTHING
        """,
        claim_id,
    )


# -------------------------------------------------
# RDF writer (BATCHED)
# -------------------------------------------------

def _write_claims_rdf_batch(
    fuseki: FusekiClient,
    rows: Iterable[dict],
) -> None:
    """
    Writes observational claim nodes into /world/liminal.

    No world membership.
    No belief assertion.
    No contradiction resolution.
    """

    blocks: list[str] = []

    for row in rows:
        claim_iri = f"urn:aios:world:claim:{row['claim_id']}"

        triples: list[str] = [
            f"<{claim_iri}> a world:Claim ;",
            f'  world:claimId "{row["claim_id"]}" ;',
            f"  world:epistemicState world:Liminal ;",
            f'  world:claimStatus "pending" ;',
            f"  world:rawText {sparql_str(row['raw_text'])} ;",
            f'  world:extractionConfidence "{row["confidence"]}"^^xsd:float ;',
        ]

        if row.get("extraction_rule"):
            triples.append(
                f"  world:extractionRule {sparql_str(row['extraction_rule'])} ;"
            )

        if row.get("extraction_ver"):
            triples.append(
                f"  world:extractionVersion {sparql_str(row['extraction_ver'])} ;"
            )

        triples.append(
            f'  world:observedAt "{row["created_at"].isoformat()}"^^xsd:dateTime ;'
        )

        if row.get("subject"):
            triples.append(f"  world:surfaceSubject {sparql_str(row['subject'])} ;")
        if row.get("predicate"):
            triples.append(f"  world:surfacePredicate {sparql_str(row['predicate'])} ;")
        if row.get("object"):
            triples.append(f"  world:surfaceObject {sparql_str(row['object'])} ;")

        if row.get("document_id"):
            doc_iri = f"urn:aios:document:{row['document_id']}"
            triples.append(f"  prov:wasDerivedFrom <{doc_iri}> ;")
            triples.append(f"  world:sourceDocument <{doc_iri}> ;")

        triples[-1] = triples[-1].rstrip(" ;") + " ."
        blocks.append("\n".join(triples))

    sparql = f"""
PREFIX world: <urn:aios:world#>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>

INSERT DATA {{
  GRAPH <{GRAPH_IRI}> {{
{chr(10).join(blocks)}
  }}
}}
"""

    fuseki.update(DATASET, sparql)


# -------------------------------------------------
# Helpers
# -------------------------------------------------

def sparql_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f"\"\"\"{escaped}\"\"\""
