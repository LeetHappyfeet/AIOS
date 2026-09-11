from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aios_app.db import Database

logger = logging.getLogger("aios.epistemic.topology_projection")

_REAL_PROJECT_SCOPE_RDF: Optional[Callable[..., Awaitable[tuple[str, str]]]] = None
_TOPOLOGY_MODULE: Any = None


def install_deferred_projection(topology_module: Any, topology_claims_module: Any) -> None:
    """Replace per-item Fuseki rewrites with a dirty-scope projection boundary.

    The topology derivation functions continue to own all node/edge/anchor
    mutation logic.  Only their expensive whole-scope RDF side effect is
    replaced.  The original projector is retained here and is invoked only by
    project_semantic_scope jobs.
    """

    global _REAL_PROJECT_SCOPE_RDF, _TOPOLOGY_MODULE
    if _REAL_PROJECT_SCOPE_RDF is None:
        _REAL_PROJECT_SCOPE_RDF = topology_module._project_scope_rdf
        _TOPOLOGY_MODULE = topology_module

    topology_module._project_scope_rdf = deferred_project_scope_rdf
    # topology_claims imported the private projector by value, so patch that
    # local binding too.  This is the existing compatibility pattern used by
    # epistemic.__init__ for claim-topology ownership resolution.
    topology_claims_module._project_scope_rdf = deferred_project_scope_rdf


async def mark_scope_dirty(db: Database, *, decision: Any) -> int:
    if _TOPOLOGY_MODULE is None:
        raise RuntimeError("semantic topology projection boundary is not installed")

    dataset, graph = _TOPOLOGY_MODULE._rdf_graph(decision)
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.semantic_scope_projection_state (
            scope_key, scope_kind, rdf_dataset, rdf_graph,
            dirty_version, projected_version, status, dirty_at
        )
        VALUES ($1,$2,$3,$4,1,0,'dirty',now())
        ON CONFLICT (scope_key) DO UPDATE
        SET scope_kind=EXCLUDED.scope_kind,
            rdf_dataset=EXCLUDED.rdf_dataset,
            rdf_graph=EXCLUDED.rdf_graph,
            dirty_version=aios.semantic_scope_projection_state.dirty_version + 1,
            status='dirty',
            dirty_at=now(),
            last_error=NULL,
            updated_at=now()
        RETURNING dirty_version
        """,
        decision.scope_key,
        decision.scope_kind,
        dataset,
        graph,
    )

    # Lazy import avoids a package-import cycle while still using the single
    # blessed pipeline enqueue path. The partial unique index coalesces races.
    from aios_app.pipeline.jobs import enqueue_job

    await enqueue_job(
        db,
        job_type="project_semantic_scope",
        payload={"scope_key": decision.scope_key},
        priority=70,
    )
    return int(row["dirty_version"])


async def deferred_project_scope_rdf(
    db: Database,
    _fuseki: Any,
    *,
    decision: Any,
) -> tuple[str, str]:
    """Record projection invalidation without reading/re-writing the RDF graph."""

    if _TOPOLOGY_MODULE is None:
        raise RuntimeError("semantic topology projection boundary is not installed")
    await mark_scope_dirty(db, decision=decision)
    return _TOPOLOGY_MODULE._rdf_graph(decision)


async def project_semantic_scope(
    db: Database,
    fuseki: Any,
    *,
    scope_key: str,
) -> dict[str, Any]:
    """Project the latest authoritative PostgreSQL topology for one scope once."""

    if _REAL_PROJECT_SCOPE_RDF is None or _TOPOLOGY_MODULE is None:
        raise RuntimeError("semantic topology projection boundary is not installed")

    state = await db.fetchrow(
        """
        SELECT scope_key, scope_kind, dirty_version, projected_version
        FROM aios.semantic_scope_projection_state
        WHERE scope_key=$1
        """,
        scope_key,
    )
    if not state:
        return {"scope_key": scope_key, "projected": False, "reason": "not_dirty"}

    target_version = int(state["dirty_version"] or 0)
    if target_version <= int(state["projected_version"] or 0):
        return {"scope_key": scope_key, "projected": False, "reason": "already_current"}

    scope = await db.fetchrow(
        """
        SELECT scope_kind, scope_key, character_id, character_instance_id,
               world_id, source_id
        FROM aios.semantic_topology_node
        WHERE scope_key=$1
        ORDER BY created_at, topology_node_id
        LIMIT 1
        """,
        scope_key,
    )
    if not scope:
        await db.execute(
            """
            UPDATE aios.semantic_scope_projection_state
            SET projected_version=dirty_version,
                status='ready', projected_at=now(), last_error=NULL,
                updated_at=now()
            WHERE scope_key=$1
            """,
            scope_key,
        )
        return {"scope_key": scope_key, "projected": False, "reason": "empty_scope"}

    decision = _TOPOLOGY_MODULE.TopologyDecision(
        scope_kind=scope["scope_kind"],
        scope_key=scope["scope_key"],
        branch_kind="scope_projection",
        significance=1.0,
        character_id=scope["character_id"],
        character_instance_id=scope["character_instance_id"],
        world_id=scope["world_id"],
        source_id=scope["source_id"],
    )

    await db.execute(
        """
        UPDATE aios.semantic_scope_projection_state
        SET status='projecting', last_error=NULL, updated_at=now()
        WHERE scope_key=$1
        """,
        scope_key,
    )

    try:
        dataset, graph = await _REAL_PROJECT_SCOPE_RDF(
            db,
            fuseki,
            decision=decision,
        )
    except Exception as exc:
        await db.execute(
            """
            UPDATE aios.semantic_scope_projection_state
            SET status='error', last_error=$2, updated_at=now()
            WHERE scope_key=$1
            """,
            scope_key,
            repr(exc)[:2000],
        )
        raise

    current = await db.execute_returning_row(
        """
        UPDATE aios.semantic_scope_projection_state
        SET rdf_dataset=$2,
            rdf_graph=$3,
            projected_version=GREATEST(projected_version,$4),
            status=CASE WHEN dirty_version <= $4 THEN 'ready' ELSE 'dirty' END,
            projected_at=now(),
            last_error=NULL,
            updated_at=now()
        WHERE scope_key=$1
        RETURNING dirty_version, projected_version, status
        """,
        scope_key,
        dataset,
        graph,
        target_version,
    )

    logger.info(
        "Projected semantic topology scope=%s version=%s dirty_now=%s status=%s",
        scope_key,
        target_version,
        current["dirty_version"],
        current["status"],
    )
    return {
        "scope_key": scope_key,
        "projected": True,
        "projected_version": int(current["projected_version"] or 0),
        "dirty_version": int(current["dirty_version"] or 0),
        "status": str(current["status"]),
    }


async def enqueue_dirty_scope_jobs(db: Database, *, limit: int = 64) -> int:
    """Ensure dirty scopes eventually receive a projection after an active job exits."""

    rows = await db.fetch(
        """
        SELECT s.scope_key
        FROM aios.semantic_scope_projection_state s
        WHERE s.dirty_version > s.projected_version
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type='project_semantic_scope'
                AND pj.status IN ('queued','running')
                AND pj.payload->>'scope_key'=s.scope_key
          )
        ORDER BY s.dirty_at NULLS FIRST, s.scope_key
        LIMIT $1
        """,
        limit,
    )
    if not rows:
        return 0

    from aios_app.pipeline.jobs import enqueue_job

    created = 0
    for row in rows:
        job_id = await enqueue_job(
            db,
            job_type="project_semantic_scope",
            payload={"scope_key": str(row["scope_key"])},
            priority=70,
        )
        created += int(job_id is not None)
    return created
