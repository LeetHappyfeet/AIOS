#aios_app/rdf/world_liminal_classifier_runner.py
from __future__ import annotations

import re

import logging
from typing import Iterable
from uuid import UUID

from ..db import Database
from .fuseki import FusekiClient
from .world_liminal_classifier import classify_claim

logger = logging.getLogger("aios.rdf.world_liminal_classifier")

DATASET = "world"
GRAPH_IRI = "urn:aios:world:liminal"


# -------------------------------------------------
# Public entrypoint (NEW)
# -------------------------------------------------

async def classify_liminal_claims(
    db: Database,
    fuseki: FusekiClient,
    *,
    batch_size: int = 200,
) -> int:
    """
    Orchestrates classification of liminal claims.

    - Fetches unclassified liminal claims
    - Applies deterministic classifier
    - Writes world:contentKind to RDF
    - Logs action in rdf_promotion_log
    """

    rows = await _fetch_unclassified(db, batch_size)

    if not rows:
        return 0

    for row in rows:
        try:
            kind = classify_claim(row)
            _write_classification(fuseki, row["claim_id"], kind)
            await _log_classification(db, row["claim_id"], kind)
        except Exception:
            logger.exception(
                "Failed classifying claim %s",
                row["claim_id"],
            )

    logger.info("Classified %d liminal claims", len(rows))
    return len(rows)


# -------------------------------------------------
# SQL
# -------------------------------------------------

async def _fetch_unclassified(db: Database, limit: int) -> Iterable[dict]:
    return await db.fetch(
        """
        SELECT
            cc.claim_id,
            cc.subject,
            cc.predicate,
            cc.object,
            cc.raw_text
        FROM aios.claim_candidate cc
        WHERE EXISTS (
            SELECT 1
            FROM aios.rdf_promotion_log rpl
            WHERE rpl.claim_id = cc.claim_id
              AND rpl.rdf_dataset = 'world'
              AND rpl.rdf_graph = 'urn:aios:world:liminal'
        )
        AND NOT EXISTS (
            SELECT 1
            FROM aios.rdf_promotion_log rpl2
            WHERE rpl2.claim_id = cc.claim_id
              AND rpl2.rdf_predicate = 'world:contentKind'
        )
        ORDER BY cc.created_at
        LIMIT $1
        """,
        limit,
    )


# -------------------------------------------------
# RDF writer
# -------------------------------------------------

def _write_classification(
    fuseki: FusekiClient,
    claim_id: UUID,
    kind: str,
) -> None:
    claim_iri = f"urn:aios:world:claim:{claim_id}"

    sparql = f"""
PREFIX world: <urn:aios:world#>

INSERT DATA {{
  GRAPH <urn:aios:world:liminal> {{
    <{claim_iri}> world:contentKind "{kind}" .
  }}
}}
"""
    fuseki.update("world", sparql)



# -------------------------------------------------
# Logging
# -------------------------------------------------

async def _log_classification(
    db: Database,
    claim_id: UUID,
    kind: str,
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
            promoted_at
        )
        VALUES (
            $1::uuid,
            'world',
            'urn:aios:world:liminal',
            'urn:aios:world:claim:' || $1::text,
            'world:contentKind',
            $2,
            'world_liminal_classifier',
            now()
        )
        ON CONFLICT DO NOTHING
        """,
        claim_id,
        kind,
    )
