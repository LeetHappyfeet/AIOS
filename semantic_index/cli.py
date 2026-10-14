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
from aios_app.topic_atlas.collector import collect_topics_once
from aios_app.topic_atlas.projection import (
    index_topics_once, project_topics_once, prune_retired_topic_vectors_once,
)
from aios_app.topic_atlas.hygiene import retire_invalid_candidates_once
from .corpus_discovery import (
    index_corpus_discovery_once, prune_deleted_corpus_once,
    link_corpus_topics_once,
)
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
from .eligibility import (
    quarantine_ineligible_vectors_once, quarantine_adjudicated_vectors_once,
    QUARANTINE_PHASES,
)
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
    total_indexed = sum(totals.get(name,0) for name in (
        "source_indexed","frames_indexed","propositions_indexed","epistemic_indexed",
        "corpus_indexed","topics_indexed"))
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
        "corpus_indexed_per_s": round(totals.get("corpus_indexed", 0) / elapsed, 2),
        "topic_coverage_per_s": round(totals.get("topic_coverage", 0) / elapsed, 2),
        "topics_collected_per_s": round(totals.get("topics_collected", 0) / elapsed, 2),
        "topics_indexed_per_s": round(totals.get("topics_indexed", 0) / elapsed, 2),
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


async def _run_vector_stages(db, cfg, run_stage, *, run_corpus: bool = True) -> dict[str, int]:
    # Admission requires a canonical proposition vector. No topology or cleanup
    # operation may be inserted into this dependency chain.
    # Explicitly adjudicated retractions must not wait behind the larger
    # topology-quarantine maintenance pass (which may yield on SQL budgets).
    await run_stage("hygiene-retirement", quarantine_adjudicated_vectors_once,
                    db, cfg, limit=8)
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
    # Cold corpus has its own collection and guaranteed background budget.
    # The v1 shared-source corpus index is retained only for legacy/manual work.
    retired_corpus = 0
    corpus = 0
    coverage = 0
    if run_corpus:
        retired_corpus = await run_stage("vector-corpus-prune", prune_deleted_corpus_once,
                                         db, cfg, limit=cfg.background_batch_size)
        corpus = await run_stage("vector-corpus-v2", index_corpus_discovery_once,
                                 db, cfg, limit=cfg.background_batch_size)
    await run_stage("vector-topic-retire", prune_retired_topic_vectors_once, db, cfg,
                    limit=cfg.background_batch_size)
    topics = await run_stage("vector-topics", index_topics_once, db, cfg,
                             limit=cfg.background_batch_size)
    if run_corpus:
        coverage = await run_stage("vector-topic-coverage", link_corpus_topics_once,
                                   db, cfg, limit=min(2,cfg.background_batch_size),
                                   min_score=cfg.corpus_topic_min_score)
    return {"propositions_indexed": propositions, "frames_indexed": frames,
            "epistemic_indexed": epistemic, "source_indexed": source,
            "corpus_indexed": corpus, "corpus_pruned": retired_corpus,
            "topic_coverage": coverage,
            "topics_indexed": topics, "admitted": admitted, "admission_bypassed": bypassed}


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
            "admitted", "admission_bypassed", "topics_collected", "topics_indexed", "corpus_indexed",
            "corpus_pruned", "topic_coverage",
            "structured", "neighbor_classified",
            "clustered", "classified", "reconciled", "validated")}
        last_batches = {}
        backlog = {}
        failed = []
        last_backlog_snapshot = 0.0
        last_topic_scan = 0.0
        last_corpus_scan = 0.0

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
            if time.monotonic() - last_topic_scan >= 10.0:
                # Topic mentions are advisory and run behind the critical vector
                # stages on a bounded cadence, not on HUD's hot path.
                found = await run_stage("topic-discovery", collect_topics_once, db,
                                        limit=cfg.background_batch_size)
                retired_bad = await run_stage(
                    "topic-hygiene", retire_invalid_candidates_once, db,
                    limit=cfg.background_batch_size)
                totals["topic_hygiene_retired"] = retired_bad
                totals["topics_collected"] = sum(found.values()) if isinstance(found,dict) else 0
                last_topic_scan = time.monotonic()
            corpus_due = time.monotonic()-last_corpus_scan >= cfg.corpus_refresh_seconds
            totals.update(await _run_vector_stages(
                db, cfg, run_stage, run_corpus=corpus_due))
            if corpus_due:
                last_corpus_scan=time.monotonic()
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
        # The general quarantine pass previously ran six unrelated global
        # queries inside one ten-second budget, then retried every five seconds
        # even at batch=1. Rotate one phase/cycle; throttle an expensive phase
        # independently without starving the other semantic maintenance lanes.
        quarantine_phases = ("vectors",) + tuple(
            phase for phase in QUARANTINE_PHASES if phase != "vectors")
        quarantine_cursor = 0
        quarantine_next_at = 0.0
        quarantine_min_interval = max(8.0, cfg.topology_sql_seconds * 2.0)
        quarantine_retry_after: dict[str, float] = {}
        quarantine_failures: dict[str, int] = {}
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
            work["topics_projected"] = await run_stage(
                "rdf-topics", project_topics_once, db, fuseki,
                limit=1)  # One acknowledged graph replacement per topology cycle.
            # Prevent historical global maintenance from occupying the SQL
            # pool on every ~2-second cycle while foreground HUD is active.
            if time.monotonic() >= quarantine_next_at:
                phase = quarantine_phases[quarantine_cursor]
                quarantine_cursor = (quarantine_cursor + 1) % len(quarantine_phases)
                stage = f"topology-quarantine/{phase}"
                if time.monotonic() >= quarantine_retry_after.get(phase, 0.0):
                    phase_batch = (effective_cfg.background_batch_size if phase == "vectors"
                                   else min(4, effective_cfg.background_batch_size))
                    await run_stage(
                        stage, quarantine_ineligible_vectors_once,
                        db, replace(effective_cfg, batch_size=phase_batch),
                        phase=phase,
                    )
                    if stage in failed:
                        streak = quarantine_failures.get(phase, 0) + 1
                        quarantine_failures[phase] = streak
                        # Query budgets and independent HUD retrieval should not
                        # be continually contended by a pathological global scan.
                        cooldown = min(300.0, 30.0 * (2 ** min(streak, 4)))
                        quarantine_retry_after[phase] = time.monotonic() + cooldown
                        logger.warning(
                            "Topology quarantine phase=%s cooldown=%.0fs after %d failures",
                            phase, cooldown, streak,
                        )
                    else:
                        quarantine_failures[phase] = 0
                        quarantine_retry_after.pop(phase, None)
                quarantine_next_at = time.monotonic() + quarantine_min_interval
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
