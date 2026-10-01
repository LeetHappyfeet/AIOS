from __future__ import annotations

import argparse
import asyncio
import asyncpg
import json
import logging
import time
from dataclasses import replace
from typing import Any

from aios_app.config import settings
from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient
from .config import SemanticIndexConfig
from .service import (
    index_source_sections_once, index_corpus_sections_once, index_semantic_frames_once,
    index_propositions_once, index_epistemic_objects_once,
    initialize_backend, initialize_topology_backend, _get_embedder, _get_store,
)
from .admission import (
    admit_semantic_neighbors_once,
    fail_open_stalled_admissions_once,
    admission_backlog_snapshot,
)
from .eligibility import quarantine_ineligible_vectors_once
from .query_server import start_query_server
from .structure import analyze_neighbors_once
from .neighbor_classifier import classify_neighbor_relations_once
from .validation_adapter import record_new_relation_decisions
from .reconciliation import (
    reconcile_neighbor_relations_once, retract_superseded_clusters_once,
    reproject_pending_reconciliation_once,
)

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
        "validated_per_s": round(totals.get("validated", 0) / elapsed, 2),
    }


async def _timed_stage(stage: str, func: Any, *args: Any, **kwargs: Any) -> Any:
    started = time.monotonic()
    try:
        result = await func(*args, **kwargs)
    except (asyncio.TimeoutError, asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError):
        logger.warning("Semantic stage reached its SQL/lock budget stage=%s elapsed=%.2fs",
                       stage,time.monotonic()-started)
        raise
    except Exception:
        logger.exception("Semantic stage failed stage=%s elapsed=%.2fs",
                         stage, time.monotonic() - started)
        raise
    elapsed = time.monotonic() - started
    if result or elapsed >= 1.0:
        logger.info("Semantic stage completed stage=%s count=%s elapsed=%.2fs",
                    stage, result, elapsed)
    return result


async def _run_topology_stages(db, fuseki, cfg, run_stage) -> dict[str, int]:
    structured = await run_stage("neighbors", analyze_neighbors_once, db, cfg)
    classified = await run_stage("neighbor-classification", classify_neighbor_relations_once, db, cfg)
    # Always drain validation, including stale work when classification is idle.
    validated = await run_stage("relation-validation", record_new_relation_decisions,
                                db, limit=cfg.validation_batch_size)
    pairs = await run_stage("pair-reconciliation", reconcile_neighbor_relations_once, db, fuseki, cfg)
    retired = await run_stage("cluster-cleanup", retract_superseded_clusters_once, db, cfg)
    projected = await run_stage("rdf-catch-up", reproject_pending_reconciliation_once, db, fuseki, cfg)
    return {"structured": int(structured), "neighbor_classified": int(classified),
            "validated": int(validated),
            "reconciled": int(pairs) + int(retired) + int(projected)}


async def analyze_regions_once() -> None:
    """Explicit legacy global region analysis; never called by ingestion.

    Scoped/incremental region jobs are a separate follow-up change.
    """
    from .clustering import cluster_neighbors_once
    from .classifier import classify_latest_clusters_once
    from .reconciliation import reconcile_clusters_once, reconcile_boundaries_once
    cfg = SemanticIndexConfig()
    db = Database(settings.db_dsn)
    fuseki = FusekiClient(settings.fuseki_base_url)
    await db.connect()
    try:
        await _timed_stage("relation-validation", record_new_relation_decisions,
                           db, limit=cfg.validation_batch_size)
        await _timed_stage("clustering", cluster_neighbors_once, db, cfg)
        await _timed_stage("cluster-classification", classify_latest_clusters_once, db, cfg)
        await _timed_stage("cluster-reconciliation", reconcile_clusters_once, db, fuseki, cfg)
        await _timed_stage("boundary-reconciliation", reconcile_boundaries_once, db, fuseki, cfg)
        await _timed_stage("cluster-cleanup", retract_superseded_clusters_once, db, cfg)
        await _timed_stage("rdf-catch-up", reproject_pending_reconciliation_once, db, fuseki, cfg)
    finally:
        await db.close()


async def _run_vector_stages(db, cfg, run_stage) -> dict[str, int]:
    # Admission requires a canonical proposition vector. No topology or cleanup
    # operation may be inserted into this dependency chain.
    propositions = await run_stage("vector-propositions", index_propositions_once, db, cfg)
    admission_cfg = replace(cfg, batch_size=cfg.admission_batch_size)
    admitted = await run_stage(
        "semantic-admission", admit_semantic_neighbors_once, db, admission_cfg,
        embedder=_get_embedder(cfg), store=_get_store(cfg, cfg.proposition_collection))
    bypassed = await run_stage("semantic-admission-repair", fail_open_stalled_admissions_once, db, cfg)
    frames = await run_stage("vector-frames", index_semantic_frames_once, db, cfg)
    epistemic = await run_stage("vector-epistemic", index_epistemic_objects_once, db, cfg)
    background_cfg = replace(cfg, batch_size=cfg.background_batch_size)
    source = await run_stage("vector-source", index_source_sections_once, db, background_cfg)
    # Cold corpus embeddings only consume a small batch after the live streams.
    corpus = 0
    if max(propositions, frames, epistemic) < cfg.batch_size:
        corpus = await run_stage("vector-corpus", index_corpus_sections_once, db, background_cfg)
    return {"propositions_indexed": propositions, "frames_indexed": frames,
            "epistemic_indexed": epistemic, "source_indexed": source + corpus,
            "admitted": admitted, "admission_bypassed": bypassed}


async def run_forever(poll_seconds: float = 1.0) -> None:
    cfg = SemanticIndexConfig()
    db = Database(settings.db_dsn, max_size=4,
                  server_settings={"statement_timeout": str(int(cfg.vector_sql_seconds * 1000)), "lock_timeout": "1000"})
    query_server = None
    await db.connect()
    try:
        initialize_backend(cfg, warmup=True)
        query_server = start_query_server(cfg)
        logger.info("AIOS_READY service=semantic_index")
        window_started = time.monotonic()
        totals = {key: 0 for key in (
            "source_indexed", "frames_indexed", "propositions_indexed", "epistemic_indexed",
            "admitted", "admission_bypassed", "structured", "neighbor_classified",
            "clustered", "classified", "reconciled", "validated")}
        last_batches = {}
        backlog = {}
        failed = []
        last_backlog_snapshot = 0.0

        async def run_stage(stage, func, *args, **kwargs):
            _emit_telemetry(_semantic_snapshot(
                state="WORKING", stage=stage, stage_started_at=time.time(), cfg=cfg,
                last_batches=last_batches, totals=totals, window_started=window_started,
                admission_backlog=backlog))
            try:
                # Bound semantic admission's per-candidate SQL/ANN work as well
                # as its batch size. Vector upserts retain receipt-after-write ordering.
                if stage.startswith("semantic-admission"):
                    return await asyncio.wait_for(
                        _timed_stage(stage,func,*args,**kwargs),
                        timeout=cfg.admission_stage_seconds)
                return await _timed_stage(stage, func, *args, **kwargs)
            except Exception as exc:
                failed.append(stage)
                if isinstance(exc,(asyncio.TimeoutError,asyncpg.QueryCanceledError,asyncpg.LockNotAvailableError)):
                    logger.warning("Vector stage deferred stage=%s reason=%s",stage,type(exc).__name__)
                else:
                    logger.exception("Vector stage deferred for retry stage=%s", stage)
                return 0

        while True:
            failed.clear()
            totals.update(await _run_vector_stages(db, cfg, run_stage))
            last_batches = dict(totals)
            if time.monotonic() - last_backlog_snapshot >= 10:
                try:
                    backlog = await admission_backlog_snapshot(db)
                except Exception:
                    failed.append("admission-backlog")
                    logger.exception("Admission backlog telemetry unavailable")
                last_backlog_snapshot = time.monotonic()
            active = any(totals.values())
            _emit_telemetry(_semantic_snapshot(
                state="DEGRADED" if failed else ("DRAINING" if active else "CAUGHT_UP"),
                stage="retry" if failed else ("cycle-complete" if active else "idle"),
                stage_started_at=None, cfg=cfg, last_batches=last_batches,
                totals=totals, window_started=window_started, admission_backlog=backlog))
            totals = {key: 0 for key in totals}
            window_started = time.monotonic()
            # Every cycle yields; outages cannot produce a CPU spin.
            await asyncio.sleep(poll_seconds)
    finally:
        if query_server is not None:
            query_server.shutdown()
            query_server.server_close()
        await db.close()


async def run_topology_forever(poll_seconds: float = 2.0) -> None:
    """Independent low-budget maintenance, with no embedding model or query port."""
    cfg = SemanticIndexConfig()
    db = Database(settings.db_dsn, max_size=2, server_settings={
        "statement_timeout": str(int(cfg.topology_sql_seconds * 1000)),
        "lock_timeout": "1000"})
    fuseki = FusekiClient(settings.fuseki_base_url, timeout=5, retries=0)
    await db.connect()
    try:
        # Launch starts this worker after the vector/query service is ready.
        initialize_topology_backend(cfg)
        logger.info("AIOS_READY service=semantic_topology")
        failed = []
        batch_sizes = {name:getattr(cfg,name) for name in (
            "neighbor_batch_size","relation_batch_size","validation_batch_size",
            "reconciliation_batch_size","background_batch_size")}
        stage_batches = {"neighbors":"neighbor_batch_size",
                         "neighbor-classification":"relation_batch_size",
                         "relation-validation":"validation_batch_size",
                         "pair-reconciliation":"reconciliation_batch_size",
                         "topology-quarantine":"background_batch_size"}

        async def run_stage(stage, func, *args, **kwargs):
            _emit_telemetry({"service": "semantic_topology", "state": "WORKING",
                             "stage": stage, "stage_started_at": time.time()})
            try:
                return await asyncio.wait_for(
                    _timed_stage(stage,func,*args,**kwargs),
                    timeout=cfg.topology_stage_seconds)
            except Exception as exc:
                failed.append(stage)
                size = stage_batches.get(stage)
                if size:
                    batch_sizes[size] = max(1,batch_sizes[size] // 2)
                if isinstance(exc,(asyncio.TimeoutError,asyncpg.QueryCanceledError,asyncpg.LockNotAvailableError)):
                    logger.warning("Topology stage yielded stage=%s reason=%s next_batch=%s",
                                   stage,type(exc).__name__,batch_sizes.get(size))
                else:
                    logger.exception("Topology stage yielded for retry stage=%s next_batch=%s",
                                     stage,batch_sizes.get(size))
                return 0

        while True:
            failed.clear()
            # Failure in candidate discovery/classification must not stop older
            # verified decisions and RDF deltas from progressing.
            effective_cfg = replace(cfg,**batch_sizes)
            work = await _run_topology_stages(db, fuseki, effective_cfg, run_stage)
            await run_stage("topology-quarantine", quarantine_ineligible_vectors_once,
                            db, replace(effective_cfg, batch_size=effective_cfg.background_batch_size))
            _emit_telemetry({"service": "semantic_topology",
                             "state": "DEGRADED" if failed else ("DRAINING" if any(work.values()) else "CAUGHT_UP"),
                             "stage": "retry" if failed else "idle", "stage_started_at": None})
            await asyncio.sleep(max(poll_seconds,5.0) if failed else poll_seconds)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="AIOS semantic index")
    parser.add_argument("--analyze-regions-once", action="store_true",
                        help="Run the legacy global region analysis explicitly, then exit")
    parser.add_argument("--topology-only", action="store_true",
                        help="Run independent bounded topology maintenance without an embedding model")
    args = parser.parse_args()
    if args.topology_only and args.analyze_regions_once:
        parser.error("--topology-only and --analyze-regions-once are mutually exclusive")
    asyncio.run(analyze_regions_once() if args.analyze_regions_once else
                run_topology_forever() if args.topology_only else run_forever())
