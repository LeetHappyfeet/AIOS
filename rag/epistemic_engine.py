#aios_app/rag/epitesmic_engine.py

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Dict, List

from aios_app.db import Database
from .rag_config import RagConfig

logger = logging.getLogger("aios.rag.epistemic_engine")


# ============================================================
# Report model
# ============================================================

@dataclass
class EpistemicReport:
    loops: int = 0
    converged: bool = False

    claims_indexed: int = 0
    similarity_edges_attempted: int = 0
    contradictions_inserted: int = 0
    split_candidates_inserted: int = 0
    worlds_promoted: int = 0
    claim_affinities_written: int = 0

    notes: List[str] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"loops={self.loops} converged={self.converged} | "
            f"claims_indexed={self.claims_indexed}, "
            f"similarity_edges_attempted={self.similarity_edges_attempted}, "
            f"contradictions_inserted={self.contradictions_inserted}, "
            f"split_candidates_inserted={self.split_candidates_inserted}, "
            f"worlds_promoted={self.worlds_promoted}, "
            f"claim_affinities_written={self.claim_affinities_written}"
        )


# ============================================================
# Safe imports (keeps supervisor clean)
# ============================================================

def _try_import(path: str, name: str):
    try:
        mod = __import__(path, fromlist=[name])
        return getattr(mod, name)
    except Exception as exc:
        logger.debug("Stage unavailable: %s.%s (%s)", path, name, exc)
        return None


_ingest_claims_once = _try_import(
    "aios_app.rag.claims.claim_ingest_worker", "ingest_once"
)
_build_similarity_edges_once = _try_import(
    "aios_app.rag.claims.claim_similarity_worker", "build_similarity_edges_once"
)
_detect_contradictions_from_edges = _try_import(
    "aios_app.rag.claims.contradiction_detector", "detect_contradictions_from_edges"
)
_seed_splits_from_sections_once = _try_import(
    "aios_app.rag.world.section_claim_cluster_worker", "seed_section_splits_once"
)
_promote_world_splits_once = _try_import(
    "aios_app.rag.world.world_split_promoter", "promote_world_splits_once"
)
_assign_claim_affinities_once = _try_import(
    "aios_app.rag.world.claim_world_affinity_worker",
    "assign_claim_affinities_once",
)


# ============================================================
# Epistemic engine
# ============================================================

async def advance_epistemic_state(
    db: Database,
    cfg: RagConfig,
    *,
    max_loops: int = 3,
    stop_if_no_progress: bool = True,
    # stage bounds
    claim_index_passes: int = 1,
    similarity_batch_size: int = 200,
    similarity_top_k: int = 50,
    similarity_threshold: float = 0.75,
    contradiction_edge_limit: int = 800,
    contradiction_min_similarity: float = 0.75,
    contradiction_min_score: float = 0.70,
) -> EpistemicReport:
    """
    Advance the epistemic pipeline in bounded passes.

    This engine:
      - orchestrates structure, not truth
      - is idempotent by design
      - is supervisor-safe
    """

    report = EpistemicReport()
    total_progress = 0

    for loop_idx in range(1, max_loops + 1):
        report.loops = loop_idx
        loop_progress = 0

        # ----------------------------------------------------
        # Stage 1: Claim indexing
        # ----------------------------------------------------
        if _ingest_claims_once is not None:
            try:
                for _ in range(max(1, claim_index_passes)):
                    n = await _ingest_claims_once(db, cfg)
                    if n <= 0:
                        break
                    report.claims_indexed += int(n)
                    loop_progress += int(n)
            except Exception as exc:
                report.notes.append(f"claim_ingest_failed: {exc!r}")
                logger.exception("claim_ingest_failed")

        # ----------------------------------------------------
        # Stage 2: Claim similarity edges
        # ----------------------------------------------------
        if _build_similarity_edges_once is not None:
            try:
                n = await _build_similarity_edges_once(
                    db,
                    cfg,
                    batch_size=similarity_batch_size,
                    top_k=similarity_top_k,
                    similarity_threshold=similarity_threshold,
                )
                report.similarity_edges_attempted += int(n)
                loop_progress += int(n)
            except TypeError:
                n = await _build_similarity_edges_once(  # type: ignore[misc]
                    db,
                    cfg,
                    batch_size=similarity_batch_size,
                )
                report.similarity_edges_attempted += int(n)
                loop_progress += int(n)
            except Exception as exc:
                report.notes.append(f"similarity_worker_failed: {exc!r}")
                logger.exception("similarity_worker_failed")

        # ----------------------------------------------------
        # Stage 3: Contradiction detection
        # ----------------------------------------------------
        if _detect_contradictions_from_edges is not None:
            try:
                n = await _detect_contradictions_from_edges(
                    db,
                    cfg,
                    min_similarity=contradiction_min_similarity,
                    min_contradiction_score=contradiction_min_score,
                    limit=contradiction_edge_limit,
                )
                report.contradictions_inserted += int(n)
                loop_progress += int(n)
            except TypeError:
                n = await _detect_contradictions_from_edges(db, cfg)  # type: ignore[misc]
                report.contradictions_inserted += int(n)
                loop_progress += int(n)
            except Exception as exc:
                report.notes.append(f"contradiction_detector_failed: {exc!r}")
                logger.exception("contradiction_detector_failed")

        # ----------------------------------------------------
        # Stage 4: Section-based world split seeding
        # ----------------------------------------------------
        if _seed_splits_from_sections_once is not None:
            try:
                n = await _seed_splits_from_sections_once(db, cfg)  # type: ignore[misc]
                report.split_candidates_inserted += int(n)
                loop_progress += int(n)
            except Exception as exc:
                report.notes.append(f"section_split_seed_failed: {exc!r}")
                logger.exception("section_split_seed_failed")
        else:
            report.notes.append("section_split_seeding_unavailable")

        # ----------------------------------------------------
        # Stage 5: World split promotion
        # ----------------------------------------------------
        if _promote_world_splits_once is not None:
            try:
                n = await _promote_world_splits_once(db, cfg)  # type: ignore[misc]
                report.worlds_promoted += int(n)
                loop_progress += int(n)
            except Exception as exc:
                report.notes.append(f"world_split_promotion_failed: {exc!r}")
                logger.exception("world_split_promotion_failed")
        else:
            report.notes.append("world_split_promotion_unavailable")

        # ----------------------------------------------------
        # Stage 6: Probabilistic claim → world affinity
        # ----------------------------------------------------
        if _assign_claim_affinities_once is not None:
            try:
                n = await _assign_claim_affinities_once(db, cfg)  # type: ignore[misc]
                report.claim_affinities_written += int(n)
                loop_progress += int(n)
            except Exception as exc:
                report.notes.append(f"claim_affinity_failed: {exc!r}")
                logger.exception("claim_affinity_failed")
        else:
            report.notes.append("claim_affinity_unavailable")

        total_progress += loop_progress

        logger.info(
            "Epistemic loop %d progress=%d | %s",
            loop_idx,
            loop_progress,
            report.summary(),
        )

        if stop_if_no_progress and loop_progress == 0:
            report.converged = True
            break

    if not report.converged:
        report.converged = (total_progress == 0)

    return report
