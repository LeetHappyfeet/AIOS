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
    index_source_sections_once, index_corpus_sections_once, index_semantic_frames_once,
    index_propositions_once, index_epistemic_objects_once,
    initialize_backend, _get_embedder, _get_store,
)
from .admission import (
    admit_semantic_neighbors_once,
    fail_open_stalled_admissions_once,
    admission_backlog_snapshot,
)
from .query_server import start_query_server
from .structure import analyze_neighbors_once
from .neighbor_classifier import classify_neighbor_relations_once
from .clustering import cluster_neighbors_once
from .classifier import classify_latest_clusters_once
from .reconciliation import reconcile_semantic_structure_once

logger = logging.getLogger("aios.semantic_index.cli")


def _emit_telemetry(payload: dict[str, Any]) -> None:
    print("AIOS_TELEMETRY " + json.dumps(payload, separators=(",", ":"), default=str), flush=True)


def _semantic_snapshot(*, state: str, stage: str, stage_started_at: float | None,
                       cfg: SemanticIndexConfig, last_batches: dict[str, int],
                       totals: dict[str, int], window_started: float,
                       admission_backlog: dict[str, int] | None = None) -> dict[str, Any]:
    elapsed = max(0.001, time.monotonic() - window_started)
    total_indexed = totals["source_indexed"] + totals["frames_indexed"] + totals["propositions_indexed"] + totals["epistemic_indexed"]
    saturated = [name for name, count in last_batches.items() if count >= cfg.batch_size]
    return {
        "service": "semantic_index", "state": state, "stage": stage,
        "stage_started_at": stage_started_at, "batch_size": cfg.batch_size,
        "last_batches": last_batches, "saturated_streams": saturated,
        "admission_backlog": admission_backlog or {"pending": 0, "overdue": 0},
        "indexed_per_s": round(total_indexed / elapsed, 2),
        "frames_per_s": round(totals["frames_indexed"] / elapsed, 2),
        "propositions_per_s": round(totals["propositions_indexed"] / elapsed, 2),
        "epistemic_per_s": round(totals["epistemic_indexed"] / elapsed, 2),
        "admitted_per_s": round(totals["admitted"] / elapsed, 2),
        "admission_bypassed_per_s": round(totals["admission_bypassed"] / elapsed, 2),
        "neighbors_per_s": round(totals["structured"] / elapsed, 2),
        "neighbor_classified_per_s": round(totals["neighbor_classified"] / elapsed, 2),
        "clusters_per_s": round(totals["clustered"] / elapsed, 2),
        "cluster_classified_per_s": round(totals["classified"] / elapsed, 2),
        "reconciled_per_s": round(totals["reconciled"] / elapsed, 2),
    }


async def run_forever(poll_seconds: float = 1.0) -> None:
    cfg = SemanticIndexConfig()
    db = Database(settings.db_dsn)
    fuseki = FusekiClient(settings.fuseki_base_url)
    query_server = None
    await db.connect()
    try:
        logger.info("Semantic Index startup: initializing embedding backend and Qdrant collections")
        initialize_backend(cfg, warmup=True)
        query_server = start_query_server(cfg)
        logger.info("AIOS_READY service=semantic_index")

        window_started = time.monotonic()
        totals = {key: 0 for key in (
            "source_indexed", "frames_indexed", "propositions_indexed", "epistemic_indexed",
            "admitted", "admission_bypassed", "structured", "neighbor_classified",
            "clustered", "classified", "reconciled"
        )}
        last_batches = {"source": 0, "frames": 0, "propositions": 0,
                        "epistemic": 0, "admission": 0, "admission_bypass": 0}
        backlog = {"pending": 0, "overdue": 0}

        async def run_stage(stage: str, func: Any, *args: Any, **kwargs: Any) -> Any:
            started_at = time.time()
            _emit_telemetry(_semantic_snapshot(
                state="WORKING", stage=stage, stage_started_at=started_at, cfg=cfg,
                last_batches=last_batches, totals=totals, window_started=window_started,
                admission_backlog=backlog,
            ))
            return await func(*args, **kwargs)

        while True:
            source_indexed = await run_stage("vector-source", index_source_sections_once, db, cfg)
            corpus_indexed = await run_stage("vector-corpus", index_corpus_sections_once, db, cfg)
            source_indexed = int(source_indexed) + int(corpus_indexed)
            last_batches["source"] = int(source_indexed); totals["source_indexed"] += int(source_indexed)
            frames_indexed = await run_stage("vector-frames", index_semantic_frames_once, db, cfg)
            last_batches["frames"] = int(frames_indexed); totals["frames_indexed"] += int(frames_indexed)
            propositions_indexed = await run_stage("vector-propositions", index_propositions_once, db, cfg)
            last_batches["propositions"] = int(propositions_indexed); totals["propositions_indexed"] += int(propositions_indexed)

            admitted = await run_stage(
                "semantic-admission", admit_semantic_neighbors_once, db, cfg,
                embedder=_get_embedder(cfg), store=_get_store(cfg, cfg.proposition_collection),
            )
            last_batches["admission"] = int(admitted); totals["admitted"] += int(admitted)

            # Repair sweep is deliberately after the normal attempt. It only
            # releases claims which have remained unclassified past the timeout.
            bypassed = await run_stage("semantic-admission-repair", fail_open_stalled_admissions_once, db, cfg)
            last_batches["admission_bypass"] = int(bypassed); totals["admission_bypassed"] += int(bypassed)
            backlog = await admission_backlog_snapshot(db)

            epistemic_indexed = await run_stage("vector-epistemic", index_epistemic_objects_once, db, cfg)
            last_batches["epistemic"] = int(epistemic_indexed); totals["epistemic_indexed"] += int(epistemic_indexed)
            indexed = source_indexed + frames_indexed + propositions_indexed + epistemic_indexed

            structured = await run_stage("neighbors", analyze_neighbors_once, db, cfg); totals["structured"] += int(structured)
            neighbor_classified = await run_stage("neighbor-classification", classify_neighbor_relations_once, db, cfg); totals["neighbor_classified"] += int(neighbor_classified)
            clustered = await run_stage("clustering", cluster_neighbors_once, db, cfg); totals["clustered"] += int(clustered)
            classified = await run_stage("cluster-classification", classify_latest_clusters_once, db, cfg); totals["classified"] += int(classified)
            reconciled = await run_stage("reconciliation", reconcile_semantic_structure_once, db, fuseki, cfg); totals["reconciled"] += int(reconciled)

            active = any(value > 0 for value in totals.values())
            state = "DRAINING" if active else "CAUGHT_UP"
            _emit_telemetry(_semantic_snapshot(
                state=state, stage="idle" if not active else "cycle-complete",
                stage_started_at=None, cfg=cfg, last_batches=last_batches,
                totals=totals, window_started=window_started,
                admission_backlog=backlog,
            ))
            totals = {key: 0 for key in totals}; window_started = time.monotonic()
            if indexed == 0 and admitted == 0 and bypassed == 0 and structured == 0 and neighbor_classified == 0 and clustered == 0 and classified == 0 and reconciled == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        if query_server is not None:
            query_server.shutdown(); query_server.server_close()
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(run_forever())
