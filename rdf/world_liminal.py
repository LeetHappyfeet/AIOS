from __future__ import annotations

import logging
from uuid import UUID
from typing import Iterable

from ..db import Database
from .fuseki import FusekiClient

logger = logging.getLogger("aios.rdf.world_liminal")

DATASET = "world"
GRAPH_IRI = "urn:aios:world:liminal"
BASE_RECEIPT_PREDICATE = "rdf:type"
BASE_RECEIPT_OBJECT = "world:Claim"


# -------------------------------------------------
# Public entrypoint (SECTION-SCOPED, BATCHED)
# -------------------------------------------------

async def promote_liminal_claims(
    db: Database,
    fuseki: FusekiClient,
    *,
    section_id: UUID,
    batch_size: int = 100,
) -> int:
    """
    Promote one document_section's claim set into RDF /world/liminal.

    The section is the completion boundary for a stored message. A successful
    RDF write is acknowledged in rdf_promotion_log per claim. Only after every
    claim belonging to the section has its base rdf:type world:Claim receipt is
    the originating ingest_event marked rdf_processed_at/process_status='done'.

    Semantics:
    - Liminal is NOT a world
    - Claims here are observational only
    - No truth, belief, or world membership is asserted
    """

    rows = list(await _fetch_claims(db, section_id, batch_size))

    if not rows:
        await _finalize_section_if_complete(db, section_id)
        return 0

    try:
        _write_claims_rdf_batch(fuseki, rows)

        for row in rows:
            await _log_promotion(db, row["claim_id"], section_id)

        await _finalize_section_if_complete(db, section_id)
    except Exception as exc:
        await _mark_section_rdf_error(db, section_id, exc)
        logger.exception(
            "Failed RDF promotion for section %s to /world/liminal",
            section_id,
        )
        # Do not swallow RDF failures. The runner must mark the pipeline job
        # failed so the supervisor can safely retry it.
        raise

    logger.info(
        "Promoted %d claims from section %s into /world/liminal",
        len(rows),
        section_id,
    )
    return len(rows)


# -------------------------------------------------
# SQL
# -------------------------------------------------

async def _fetch_claims(
    db: Database,
    section_id: UUID,
    limit: int,
) -> Iterable[dict]:
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
        JOIN aios.extracted_sentence es
          ON es.sentence_id = cc.sentence_id
        LEFT JOIN aios.claim_provenance cp
          ON cp.claim_id = cc.claim_id
        WHERE es.section_id = $1
          AND NOT EXISTS (
              SELECT 1
              FROM aios.rdf_promotion_log rpl
              WHERE rpl.claim_id = cc.claim_id
                AND rpl.rdf_dataset = $2
                AND rpl.rdf_graph = $3
                AND rpl.rdf_predicate = $4
                AND rpl.rdf_object = $5
          )
        ORDER BY es.sentence_index, cc.created_at
        LIMIT $6
        """,
        section_id,
        DATASET,
        GRAPH_IRI,
        BASE_RECEIPT_PREDICATE,
        BASE_RECEIPT_OBJECT,
        limit,
    )


async def _log_promotion(
    db: Database,
    claim_id: UUID,
    section_id: UUID,
) -> None:
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
            promoted_at,
            promotion_meta
        )
        VALUES (
            $1::uuid,
            $2,
            $3,
            'urn:aios:world:claim:' || $1::text,
            $4,
            $5,
            'world_liminal_writer',
            now(),
            jsonb_build_object('section_id', $6::text)
        )
        ON CONFLICT (claim_id, rdf_dataset, rdf_graph, rdf_predicate) DO NOTHING
        """,
        claim_id,
        DATASET,
        GRAPH_IRI,
        BASE_RECEIPT_PREDICATE,
        BASE_RECEIPT_OBJECT,
        section_id,
    )


async def _finalize_section_if_complete(
    db: Database,
    section_id: UUID,
) -> bool:
    """
    Mark the source ingest event done iff no unacknowledged base claims remain.

    Sections containing no extracted sentences/claims are terminal too: there
    is no RDF content to write, but all eligible RDF work is complete.
    """

    row = await db.fetchrow(
        """
        SELECT
            n.event_id,
            EXISTS (
                SELECT 1
                FROM aios.claim_candidate cc
                JOIN aios.extracted_sentence es
                  ON es.sentence_id = cc.sentence_id
                WHERE es.section_id = ds.section_id
                  AND NOT EXISTS (
                      SELECT 1
                      FROM aios.rdf_promotion_log rpl
                      WHERE rpl.claim_id = cc.claim_id
                        AND rpl.rdf_dataset = $2
                        AND rpl.rdf_graph = $3
                        AND rpl.rdf_predicate = $4
                        AND rpl.rdf_object = $5
                  )
            ) AS has_unpromoted_claims
        FROM aios.document_section ds
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        WHERE ds.section_id = $1
        """,
        section_id,
        DATASET,
        GRAPH_IRI,
        BASE_RECEIPT_PREDICATE,
        BASE_RECEIPT_OBJECT,
    )

    if not row:
        raise RuntimeError(f"Cannot finalize missing section {section_id}")

    if row["has_unpromoted_claims"]:
        return False

    await db.execute(
        """
        UPDATE aios.ingest_event
        SET rdf_processed_at = COALESCE(rdf_processed_at, now()),
            process_status = 'done',
            processed_at = COALESCE(processed_at, now()),
            process_error = NULL,
            rdf_error = NULL
        WHERE event_id = $1
        """,
        row["event_id"],
    )
    return True


async def _mark_section_rdf_error(
    db: Database,
    section_id: UUID,
    exc: Exception,
) -> None:
    await db.execute(
        """
        UPDATE aios.ingest_event ie
        SET process_status = 'error',
            process_error = $2,
            rdf_error = $2,
            processed_at = NULL
        FROM aios.document_section ds
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        WHERE ds.section_id = $1
          AND ie.event_id = n.event_id
        """,
        section_id,
        repr(exc)[:2000],
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
            "  world:epistemicState world:Liminal ;",
            '  world:claimStatus "pending" ;',
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
    return f'"""{escaped}"""'
