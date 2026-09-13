#aios_app/rdf/world_liminal_classifier_runner.py
from __future__ import annotations

import re

import logging
from typing import Iterable
from uuid import UUID

from ..db import Database
from .fuseki import FusekiClient
from .liminal_compaction import compact_liminal_claims
from .world_liminal_classifier import classify_claim

logger = logging.getLogger("aios.rdf.world_liminal_classifier")

DATASET = "world"
GRAPH_IRI = "urn:aios:world:liminal"


# -------------------------------------------------
# Public entrypoint
# -------------------------------------------------

async def classify_liminal_claims(
    db: Database,
    fuseki: FusekiClient,
    *,
    batch_size: int = 200,
) -> int:
    """Classify active liminal claims and compact terminal projections.

    Classification remains deterministic and observational.  After the batch,
    claims that upstream semantic reconciliation has marked absorbed, promoted,
    rejected, or historical are removed from the active liminal RDF graph.
    Their PostgreSQL claims, observations, propositions and provenance remain.
    """

    rows = await _fetch_unclassified(db, batch_size)
    classified = 0

    for row in rows:
        try:
            kind = classify_claim(row)
            _write_classification(fuseki, row["claim_id"], kind)
            await _log_classification(db, row["claim_id"], kind)
            classified += 1
        except Exception:
            logger.exception(
                "Failed classifying claim %s",
                row["claim_id"],
            )

    # Liminal cleanup is deliberately downstream of semantic lifecycle state.
    # It never decides whether evidence is redundant; it only enacts the
    # decision already materialized by the consolidation layer.
    compacted = await compact_liminal_claims(
        db,
        fuseki,
        batch_size=max(batch_size, 500),
    )

    if classified or compacted:
        logger.info(
            "Liminal maintenance classified=%d compacted=%d",
            classified,
            compacted,
        )
    return classified


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
