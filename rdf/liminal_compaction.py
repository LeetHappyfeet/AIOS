from __future__ import annotations

import logging
from typing import Any

from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient

logger = logging.getLogger("aios.rdf.liminal_compaction")

DATASET = "world"
GRAPH_IRI = "urn:aios:world:liminal"
DEFAULT_BATCH_SIZE = 500


async def compact_liminal_claims(
    db: Database,
    fuseki: FusekiClient,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Remove terminal claim projections from /world/liminal.

    PostgreSQL remains the provenance authority.  This only removes the active
    RDF staging representation for claims that the semantic lifecycle marks as
    absorbed, promoted, rejected, or historical.  Reconciliation state and raw
    claim/observation rows are never deleted here.
    """

    rows = await db.fetch(
        """
        SELECT claim_id, lifecycle_status, absorbed_into_surface_key, reason
        FROM aios.liminal_compaction_candidate
        ORDER BY updated_at, claim_id
        LIMIT $1
        """,
        max(1, min(int(batch_size), 5000)),
    )
    if not rows:
        return 0

    claim_ids = [row["claim_id"] for row in rows]
    values = " ".join(
        f"<urn:aios:world:claim:{claim_id}>" for claim_id in claim_ids
    )

    # Remove both outgoing claim description triples and any incoming references
    # from the liminal graph.  Other graphs are intentionally untouched.
    sparql = f"""
DELETE {{
  GRAPH <{GRAPH_IRI}> {{ ?claim ?p ?o . }}
}}
WHERE {{
  VALUES ?claim {{ {values} }}
  GRAPH <{GRAPH_IRI}> {{ ?claim ?p ?o . }}
}};
DELETE {{
  GRAPH <{GRAPH_IRI}> {{ ?s ?p ?claim . }}
}}
WHERE {{
  VALUES ?claim {{ {values} }}
  GRAPH <{GRAPH_IRI}> {{ ?s ?p ?claim . }}
}}
"""
    fuseki.update(DATASET, sparql)

    await db.execute(
        """
        UPDATE aios.rdf_promotion_log
        SET promotion_meta = COALESCE(promotion_meta,'{}'::jsonb)
            || jsonb_build_object(
                'compacted_at', now(),
                'compaction_policy', 'semantic-memory-surface-v1'
            )
        WHERE claim_id = ANY($1::uuid[])
          AND rdf_dataset=$2
          AND rdf_graph=$3
        """,
        claim_ids,
        DATASET,
        GRAPH_IRI,
    )

    logger.info(
        "Compacted %d terminal claims from /world/liminal",
        len(claim_ids),
    )
    return len(claim_ids)


async def liminal_compaction_backlog(db: Database) -> dict[str, Any]:
    """Return a small operational snapshot for tests/telemetry."""
    rows = await db.fetch(
        """
        SELECT lifecycle_status, COUNT(*) AS count
        FROM aios.liminal_compaction_candidate
        GROUP BY lifecycle_status
        ORDER BY lifecycle_status
        """
    )
    by_status = {str(row["lifecycle_status"]): int(row["count"]) for row in rows}
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
    }
