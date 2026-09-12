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
from .service import (
    index_source_sections_once,
    index_semantic_frames_once,
    index_propositions_once,
    index_epistemic_objects_once,
    initialize_backend,
)
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
            "source_indexed": 0,
            "frames_indexed": 0,
            "propositions_indexed": 0,
            "epistemic_indexed": 0,
            "structured": 0,
            "neighbor_classified": 0,
            "clustered": 0,
            "classified": 0,
            "reconciled": 0,
        }
        last_batches = {
            "source": 0,
            "frames": 0,
            "propositions": 0,
            "epistemic": 0,
        }

        while True:
            source_indexed = await index_source_sections_once(db, cfg)
            frames_indexed = await index_semantic_frames_once(db, cfg)
            propositions_indexed = await index_propositions_once(db, cfg)
            epistemic_indexed = await index_epistemic_objects_once(db, cfg)
            indexed = (
                source_indexed
                + frames_indexed
                + propositions_indexed
                + epistemic_indexed
            )

            last_batches = {
                "source": int(source_indexed),
                "frames": int(frames_indexed),
                "propositions": int(propositions_indexed),
                "epistemic": int(epistemic_indexed),
            }
            totals["source_indexed"] += int(source_indexed)
            totals["frames_indexed"] += int(frames_indexed)
            totals["propositions_indexed"] += int(propositions_indexed)
            totals["epistemic_indexed"] += int(epistemic_indexed)

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

            totals["structured"] += int(structured)
            totals["neighbor_classified"] += int(neighbor_classified)
            totals["clustered"] += int(clustered)
            totals["classified"] += int(classified)
            totals["reconciled"] += int(reconciled)

            now = time.monotonic()
            if now - last_report >= 5.0:
                elapsed = max(0.001, now - window_started)
                saturated = [
                    name
                    for name, count in last_batches.items()
                    if count >= cfg.batch_size
                ]
                active = any(value > 0 for value in totals.values())
                state = "CAUGHT_UP" if not active else "DRAINING"
                total_indexed = (
                    totals["source_indexed"]
                    + totals["frames_indexed"]
                    + totals["propositions_indexed"]
                    + totals["epistemic_indexed"]
                )
                _emit_telemetry(
                    {
                        "service": "semantic_index",
                        "state": state,
                        "batch_size": cfg.batch_size,
                        "last_batches": last_batches,
                        "saturated_streams": saturated,
                        "indexed_per_s": round(total_indexed / elapsed, 2),
                        "frames_per_s": round(totals["frames_indexed"] / elapsed, 2),
                        "propositions_per_s": round(
                            totals["propositions_indexed"] / elapsed, 2
                        ),
                        "epistemic_per_s": round(
                            totals["epistemic_indexed"] / elapsed, 2
                        ),
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
