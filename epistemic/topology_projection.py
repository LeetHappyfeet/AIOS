from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

from aios_app.db import Database

logger = logging.getLogger("aios.epistemic.topology_projection")

_REAL_PROJECT_SCOPE_RDF: Optional[Callable[..., Awaitable[tuple[str, str]]]] = None
_TOPOLOGY_MODULE: Any = None

RDF_UPDATE_TARGET_BYTES = 2 * 1024 * 1024
RDF_DB_PAGE_SIZE = 2000


def install_deferred_projection(topology_module: Any, topology_claims_module: Any) -> None:
    """Replace per-item Fuseki rewrites with a dirty-scope projection boundary.

    Topology derivation remains authoritative in PostgreSQL. RDF publication is
    coalesced into project_semantic_scope jobs, where large scopes are rebuilt
    through a bounded staging graph before the live graph is replaced.
    """

    global _REAL_PROJECT_SCOPE_RDF, _TOPOLOGY_MODULE
    if _REAL_PROJECT_SCOPE_RDF is None:
        _REAL_PROJECT_SCOPE_RDF = topology_module._project_scope_rdf
        _TOPOLOGY_MODULE = topology_module

    topology_module._project_scope_rdf = deferred_project_scope_rdf
    # topology_claims imported the private projector by value, so patch that
    # local binding too. This is the existing compatibility pattern used by
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


def _node_triples(scope_iri: str, row: Any) -> list[str]:
    node_iri = f"urn:aios:topology-node:{row['topology_node_id']}"
    triples = [
        f"<{scope_iri}> <urn:aios:topology#hasNode> <{node_iri}> .",
        f"<{node_iri}> <urn:aios:topology#nodeType> {json.dumps(row['node_type'])} .",
        f"<{node_iri}> <urn:aios:topology#nodeKey> {json.dumps(row['node_key'])} .",
    ]
    if row["label"]:
        triples.append(
            f"<{node_iri}> <urn:aios:topology#label> {json.dumps(row['label'])} ."
        )
    return triples


def _edge_triples(scope_iri: str, row: Any) -> list[str]:
    parent_iri = f"urn:aios:topology-node:{row['parent_node_id']}"
    child_iri = f"urn:aios:topology-node:{row['child_node_id']}"
    edge_iri = f"urn:aios:topology-edge:{row['edge_id']}"
    pred = quote(str(row["edge_type"]), safe="")
    triples = [
        f"<{parent_iri}> <urn:aios:topology#{pred}> <{child_iri}> .",
        f"<{scope_iri}> <urn:aios:topology#hasEdge> <{edge_iri}> .",
        f"<{edge_iri}> <urn:aios:topology#fromNode> <{parent_iri}> .",
        f"<{edge_iri}> <urn:aios:topology#toNode> <{child_iri}> .",
        (
            f"<{edge_iri}> <urn:aios:topology#edgeType> "
            f"{json.dumps(str(row['edge_type']))} ."
        ),
        (
            f"<{edge_iri}> <urn:aios:topology#inferenceSource> "
            f"{json.dumps(str(row['inference_source'] or 'deterministic'))} ."
        ),
        (
            f"<{edge_iri}> <urn:aios:topology#inferenceStatus> "
            f"{json.dumps(str(row['inference_status'] or 'accepted'))} ."
        ),
    ]
    if row["inference_confidence"] is not None:
        triples.append(
            f"<{edge_iri}> <urn:aios:topology#inferenceConfidence> "
            f"\"{float(row['inference_confidence'])}\"^^"
            f"<http://www.w3.org/2001/XMLSchema#double> ."
        )
    if row["edge_meta"]:
        triples.append(
            f"<{edge_iri}> <urn:aios:topology#inferenceMeta> "
            f"{json.dumps(json.dumps(_TOPOLOGY_MODULE._json_object(row['edge_meta']), sort_keys=True))} ."
        )
    return triples


def _anchor_triples(scope_iri: str, anchor: Any) -> list[str]:
    source_iri = f"urn:aios:topology-node:{anchor['source_node_id']}"
    target_iri = f"urn:aios:topology-node:{anchor['target_node_id']}"
    edge_iri = f"urn:aios:semantic-anchor:{anchor['anchor_edge_id']}"
    pred = quote(str(anchor["relationship_type"]), safe="")
    return [
        f"<{source_iri}> <urn:aios:anchor#{pred}> <{target_iri}> .",
        f"<{scope_iri}> <urn:aios:anchor#hasAnchor> <{edge_iri}> .",
        f"<{edge_iri}> <urn:aios:anchor#fromNode> <{source_iri}> .",
        f"<{edge_iri}> <urn:aios:anchor#toNode> <{target_iri}> .",
        (
            f"<{edge_iri}> <urn:aios:anchor#targetScope> "
            f"{json.dumps(str(anchor['target_scope_key']))} ."
        ),
        (
            f"<{edge_iri}> <urn:aios:anchor#relationshipType> "
            f"{json.dumps(str(anchor['relationship_type']))} ."
        ),
        (
            f"<{edge_iri}> <urn:aios:anchor#confidence> "
            f"\"{float(anchor['confidence'])}\"^^"
            f"<http://www.w3.org/2001/XMLSchema#double> ."
        ),
        (
            f"<{edge_iri}> <urn:aios:anchor#inferenceSource> "
            f"{json.dumps(str(anchor['inference_source']))} ."
        ),
        (
            f"<{edge_iri}> <urn:aios:anchor#inferenceStatus> "
            f"{json.dumps(str(anchor['inference_status']))} ."
        ),
    ]


class _RdfBatchWriter:
    def __init__(
        self,
        fuseki: Any,
        *,
        dataset: str,
        graph: str,
        target_bytes: int = RDF_UPDATE_TARGET_BYTES,
    ):
        self.fuseki = fuseki
        self.dataset = dataset
        self.graph = graph
        self.target_bytes = target_bytes
        self._triples: list[str] = []
        self._bytes = 0
        self.batch_count = 0
        self.triple_count = 0
        self.serialized_bytes = 0

    def add(self, triple: str) -> None:
        triple_bytes = len(triple.encode("utf-8")) + 1
        if self._triples and self._bytes + triple_bytes > self.target_bytes:
            self.flush()
        self._triples.append(triple)
        self._bytes += triple_bytes
        self.triple_count += 1
        self.serialized_bytes += triple_bytes
        if self._bytes >= self.target_bytes:
            self.flush()

    def extend(self, triples: list[str]) -> None:
        for triple in triples:
            self.add(triple)

    def flush(self) -> None:
        if not self._triples:
            return
        sparql = (
            f"INSERT DATA {{ GRAPH <{self.graph}> {{ "
            + " ".join(self._triples)
            + " } }"
        )
        self.fuseki.update(self.dataset, sparql)
        self.batch_count += 1
        self._triples.clear()
        self._bytes = 0


async def _project_scope_rdf_batched(
    db: Database,
    fuseki: Any,
    *,
    decision: Any,
    target_version: int,
) -> tuple[str, str]:
    """Rebuild one RDF scope with bounded requests and atomic live publication."""

    dataset, graph = _TOPOLOGY_MODULE._rdf_graph(decision)
    staging_graph = f"{graph}:staging:{target_version}"
    scope_iri = f"urn:aios:topology-scope:{quote(decision.scope_key, safe='')}"

    # A failed prior attempt at this version may have left staging data.
    fuseki.update(dataset, f"CLEAR SILENT GRAPH <{staging_graph}>")

    writer = _RdfBatchWriter(
        fuseki,
        dataset=dataset,
        graph=staging_graph,
    )
    writer.extend(
        [
            f"<{scope_iri}> <urn:aios:topology#scopeKind> {json.dumps(decision.scope_kind)} .",
            f"<{scope_iri}> <urn:aios:topology#scopeKey> {json.dumps(decision.scope_key)} .",
        ]
    )

    node_count = 0
    edge_count = 0
    anchor_count = 0

    try:
        last_node_id = None
        while True:
            rows = await db.fetch(
                """
                SELECT topology_node_id, node_type, node_key, label
                FROM aios.semantic_topology_node
                WHERE scope_key=$1
                  AND ($2::uuid IS NULL OR topology_node_id > $2::uuid)
                ORDER BY topology_node_id
                LIMIT $3
                """,
                decision.scope_key,
                last_node_id,
                RDF_DB_PAGE_SIZE,
            )
            if not rows:
                break
            for row in rows:
                writer.extend(_node_triples(scope_iri, row))
            node_count += len(rows)
            last_node_id = rows[-1]["topology_node_id"]

        last_edge_id = None
        while True:
            rows = await db.fetch(
                """
                SELECT edge_id, parent_node_id, child_node_id, edge_type,
                       inference_source, inference_status, inference_confidence,
                       meta AS edge_meta
                FROM aios.semantic_topology_edge
                WHERE scope_key=$1
                  AND ($2::uuid IS NULL OR edge_id > $2::uuid)
                ORDER BY edge_id
                LIMIT $3
                """,
                decision.scope_key,
                last_edge_id,
                RDF_DB_PAGE_SIZE,
            )
            if not rows:
                break
            for row in rows:
                writer.extend(_edge_triples(scope_iri, row))
            edge_count += len(rows)
            last_edge_id = rows[-1]["edge_id"]

        last_anchor_id = None
        while True:
            rows = await db.fetch(
                """
                SELECT anchor_edge_id, source_node_id, target_node_id,
                       relationship_type, target_scope_key, confidence,
                       inference_source, inference_status
                FROM aios.semantic_anchor_edge
                WHERE source_scope_key=$1
                  AND ($2::uuid IS NULL OR anchor_edge_id > $2::uuid)
                ORDER BY anchor_edge_id
                LIMIT $3
                """,
                decision.scope_key,
                last_anchor_id,
                RDF_DB_PAGE_SIZE,
            )
            if not rows:
                break
            for anchor in rows:
                writer.extend(_anchor_triples(scope_iri, anchor))
            anchor_count += len(rows)
            last_anchor_id = rows[-1]["anchor_edge_id"]

        writer.flush()

        # COPY replaces the destination graph. The old live graph is untouched
        # until every staging batch has succeeded.
        fuseki.update(
            dataset,
            (
                f"COPY SILENT GRAPH <{staging_graph}> TO GRAPH <{graph}>; "
                f"CLEAR SILENT GRAPH <{staging_graph}>"
            ),
        )
    except Exception:
        try:
            fuseki.update(dataset, f"CLEAR SILENT GRAPH <{staging_graph}>")
        except Exception:
            logger.warning(
                "Failed to clean RDF staging graph dataset=%s graph=%s",
                dataset,
                staging_graph,
                exc_info=True,
            )
        raise

    logger.info(
        "Projected RDF scope=%s dataset=%s version=%s nodes=%s edges=%s anchors=%s "
        "triples=%s batches=%s bytes=%s",
        decision.scope_key,
        dataset,
        target_version,
        node_count,
        edge_count,
        anchor_count,
        writer.triple_count,
        writer.batch_count,
        writer.serialized_bytes,
    )
    return dataset, graph


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
        dataset, graph = await _project_scope_rdf_batched(
            db,
            fuseki,
            decision=decision,
            target_version=target_version,
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
