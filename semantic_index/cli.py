from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aios_app.config import settings
from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient
from .config import SemanticIndexConfig
from .service import index_once, initialize_backend
from .structure import analyze_neighbors_once
from .neighbor_classifier import classify_neighbor_relations_once
from .clustering import cluster_neighbors_once
from .classifier import classify_latest_clusters_once
from .reconciliation import reconcile_semantic_structure_once

logger = logging.getLogger("aios.semantic_index.cli")


def _emit_telemetry(payload: dict[str, Any]) -> None:
    print(
        "AIOS_TELEMETRY " + json.dumps(payload, separators=(",", ":"), default=str),
        flush=True,
    )


async def _capped_vector_backlog(
    db: Database,
    cfg: SemanticIndexConfig,
    *,
    cap: int = 10000,
) -> dict[str, int | bool]:
    """Return bounded backlog counts without forcing unbounded COUNT scans."""
    frames = await db.fetchrow(
        """
        SELECT COUNT(*)::integer AS n
        FROM (
            SELECT 1
            FROM aios.claim_semantic_frame f
            WHERE f.decomposer_version='semantic-frame-v2'
              AND NOT EXISTS (
                  SELECT 1
                  FROM aios.semantic_vector_index_state s
                  WHERE s.object_type='semantic_frame'
                    AND s.object_key=f.frame_id::text
                    AND s.qdrant_collection=$1
                    AND s.embedding_model=$2
                    AND s.embedding_version=$3
              )
            LIMIT $4
        ) pending
        """,
        cfg.frame_collection,
        cfg.embedding_model,
        cfg.embedding_version,
        cap + 1,
    )
    propositions = await db.fetchrow(
        """
        SELECT COUNT(*)::integer AS n
        FROM (
            SELECT 1
            FROM aios.proposition p
            WHERE NOT EXISTS (
                SELECT 1
                FROM aios.semantic_vector_index_state s
                WHERE s.object_type='proposition'
                  AND s.object_key=p.proposition_id::text
                  AND s.qdrant_collection=$1
                  AND s.embedding_model=$2
                  AND s.embedding_version=$3
            )
            LIMIT $4
        ) pending
        """,
        cfg.proposition_collection,
        cfg.embedding_model,
        cfg.embedding_version,
        cap + 1,
    )
    frame_n = int((frames["n"] if frames else 0) or 0)
    proposition_n = int((propositions["n"] if propositions else 0) or 0)
    return {
        "frames": min(frame_n, cap),
        "frames_capped": frame_n > cap,
        "propositions": min(proposition_n, cap),
        "propositions_capped": proposition_n > cap,
    }


async def run_forever(poll_seconds: float = 1.0) -> None:
    cfg = SemanticIndexConfig()
    db = Database(settings.db_dsn)
    fuseki = FusekiClient(settings.fuseki_base_url)
    await db.connect()
    try:
        logger.info("Semantic Index startup: initializing embedding backend and Qdrant collections")
        initialize_backend(cfg, warmup=True)
        logger.info("AIOS_READY service=semantic_index")

        last_report = 0.0
        window_started = time.monotonic()
        totals = {
            "indexed": 0,
            "structured": 0,
            "neighbor_classified": 0,
            "clustered": 0,
            "classified": 0,
            "reconciled": 0,
        }

        while True:
            indexed = await index_once(db, cfg)
            structured = await analyze_neighbors_once(db, cfg)
            neighbor_classified = await classify_neighbor_relations_once(db, cfg)

            # Clustering is watermark-driven and excludes classified
            # CONTRADICTS relations from semantic glue. Do not wait for the
            # global neighbor-classification queue to drain: on a live system
            # that queue may never reach zero, which would starve clustering
            # and reconciliation indefinitely.
            clustered = await cluster_neighbors_once(db, cfg)
            classified = await classify_latest_clusters_once(db, cfg)

            reconciled = await reconcile_semantic_structure_once(
                db,
                fuseki,
                cfg,
            )

            totals["indexed"] += int(indexed)
            totals["structured"] += int(structured)
            totals["neighbor_classified"] += int(neighbor_classified)
            totals["clustered"] += int(clustered)
            totals["classified"] += int(classified)
            totals["reconciled"] += int(reconciled)

            now = time.monotonic()
            if now - last_report >= 5.0:
                elapsed = max(0.001, now - window_started)
                try:
                    backlog = await _capped_vector_backlog(db, cfg)
                    pending_vectors = int(backlog["frames"]) + int(backlog["propositions"])
                    capped = bool(backlog["frames_capped"] or backlog["propositions_capped"])
                    active = any(value > 0 for value in totals.values())
                    state = "CAUGHT_UP" if pending_vectors == 0 and not active else "DRAINING"
                    _emit_telemetry(
                        {
                            "service": "semantic_index",
                            "state": state,
                            "pending_vectors": pending_vectors,
                            "pending_vectors_capped": capped,
                            "pending_frames": backlog["frames"],
                            "pending_propositions": backlog["propositions"],
                            "indexed_per_s": round(totals["indexed"] / elapsed, 2),
                            "neighbors_per_s": round(totals["structured"] / elapsed, 2),
                            "neighbor_classified_per_s": round(
                                totals["neighbor_classified"] / elapsed, 2
                            ),
                            "clusters_per_s": round(totals["clustered"] / elapsed, 2),
                            "cluster_classified_per_s": round(
                                totals["classified"] / elapsed, 2
                            ),
                            "reconciled_per_s": round(totals["reconciled"] / elapsed, 2),
                        }
                    )
                except Exception:
                    logger.exception("Failed to collect semantic index telemetry")
                totals = {key: 0 for key in totals}
                window_started = now
                last_report = now

            if (
                indexed == 0
                and structured == 0
                and neighbor_classified == 0
                and clustered == 0
                and classified == 0
                and reconciled == 0
            ):
                await asyncio.sleep(poll_seconds)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(run_forever())
