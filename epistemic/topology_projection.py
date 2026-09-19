from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

from aios_app.db import Database

logger = logging.getLogger("aios.epistemic.topology_projection")

_REAL_PROJECT_SCOPE_RDF: Optional[Callable[..., Awaitable[tuple[str, str]]]] = None
_TOPOLOGY_MODULE: Any = None

RDF_UPDATE_TARGET_BYTES = 2 * 1024 * 1024
RDF_DELTA_TARGET_BYTES = 512 * 1024
RDF_DELTA_TARGET_OBJECTS = 128
RDF_DB_PAGE_SIZE = 2000
RDF_PROJECTION_QUIET_SECONDS = 3.0
RDF_PROJECTION_PRIORITY = 200


def install_deferred_projection(topology_module: Any, topology_claims_module: Any) -> None:
    """Replace per-item Fuseki rewrites with a dirty-scope projection boundary.

    Topology derivation remains authoritative in PostgreSQL. RDF publication is
    coalesced into project_semantic_scope jobs. Once a scope has a synchronized
    baseline, normal publication consumes the PostgreSQL mutation outbox and
    touches only changed RDF resources. Full staged rebuilds remain the
    bootstrap/repair path.
    """

    global _REAL_PROJECT_SCOPE_RDF, _TOPOLOGY_MODULE
    if _REAL_PROJECT_SCOPE_RDF is None:
        _REAL_PROJECT_SCOPE_RDF = topology_module._project_scope_rdf
        _TOPOLOGY_MODULE = topology_module

    topology_module._project_scope_rdf = deferred_project_scope_rdf
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

    run_after = datetime.now(timezone.utc) + timedelta(seconds=RDF_PROJECTION_QUIET_SECONDS)
    await enqueue_job(
        db,
        job_type="project_semantic_scope",
        payload={"scope_key": decision.scope_key},
        priority=RDF_PROJECTION_PRIORITY,
        run_after=run_after,
    )

    # If a projection is already queued, every new mutation extends the quiet
    # period. Running projections are left alone; once they finish, the dirty
    # scope recovery scan will enqueue the latest version after it goes quiet.
    await db.execute(
        """
        UPDATE aios.pipeline_job
        SET run_after=GREATEST(run_after,$2),
            priority=GREATEST(priority,$3),
            updated_at=now()
        WHERE job_type='project_semantic_scope'
          AND status='queued'
          AND payload->>'scope_key'=$1
        """,
        decision.scope_key,
        run_after,
        RDF_PROJECTION_PRIORITY,
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



def _scope_iri(scope_key: str) -> str:
    return f"urn:aios:topology-scope:{quote(scope_key, safe='')}"


def _delete_change_sparql(graph: str, scope_iri: str, change: Any) -> str:
    kind = str(change["object_kind"])
    object_id = change["object_id"]
    old = _TOPOLOGY_MODULE._json_object(change["old_state"])

    if kind == "node":
        iri = f"urn:aios:topology-node:{object_id}"
        return (
            f"DELETE WHERE {{ GRAPH <{graph}> {{ <{scope_iri}> "
            f"<urn:aios:topology#hasNode> <{iri}> . }} }}; "
            f"DELETE WHERE {{ GRAPH <{graph}> {{ <{iri}> ?p ?o . }} }}"
        )

    if kind == "edge":
        iri = f"urn:aios:topology-edge:{object_id}"
        statements = [
            (
                f"DELETE WHERE {{ GRAPH <{graph}> {{ <{scope_iri}> "
                f"<urn:aios:topology#hasEdge> <{iri}> . }} }}"
            ),
            f"DELETE WHERE {{ GRAPH <{graph}> {{ <{iri}> ?p ?o . }} }}",
        ]
        if old.get("parent_node_id") and old.get("child_node_id") and old.get("edge_type"):
            parent = f"urn:aios:topology-node:{old['parent_node_id']}"
            child = f"urn:aios:topology-node:{old['child_node_id']}"
            pred = quote(str(old["edge_type"]), safe="")
            statements.append(
                f"DELETE DATA {{ GRAPH <{graph}> {{ <{parent}> "
                f"<urn:aios:topology#{pred}> <{child}> . }} }}"
            )
        return "; ".join(statements)

    iri = f"urn:aios:semantic-anchor:{object_id}"
    statements = [
        (
            f"DELETE WHERE {{ GRAPH <{graph}> {{ <{scope_iri}> "
            f"<urn:aios:anchor#hasAnchor> <{iri}> . }} }}"
        ),
        f"DELETE WHERE {{ GRAPH <{graph}> {{ <{iri}> ?p ?o . }} }}",
    ]
    if old.get("source_node_id") and old.get("target_node_id") and old.get("relationship_type"):
        source = f"urn:aios:topology-node:{old['source_node_id']}"
        target = f"urn:aios:topology-node:{old['target_node_id']}"
        pred = quote(str(old["relationship_type"]), safe="")
        statements.append(
            f"DELETE DATA {{ GRAPH <{graph}> {{ <{source}> "
            f"<urn:aios:anchor#{pred}> <{target}> . }} }}"
        )
    return "; ".join(statements)


async def _fetch_current_delta_rows(
    db: Database,
    *,
    object_ids: dict[str, set[Any]],
) -> dict[tuple[str, Any], Any]:
    """Bulk-read the final PostgreSQL representation of changed topology objects."""

    current: dict[tuple[str, Any], Any] = {}
    node_ids = list(object_ids["node"])
    edge_ids = list(object_ids["edge"])
    anchor_ids = list(object_ids["anchor"])

    if node_ids:
        rows = await db.fetch(
            """
            SELECT topology_node_id, node_type, node_key, label
            FROM aios.semantic_topology_node
            WHERE topology_node_id = ANY($1::uuid[])
            """,
            node_ids,
        )
        current.update((("node", row["topology_node_id"]), row) for row in rows)

    if edge_ids:
        rows = await db.fetch(
            """
            SELECT edge_id, parent_node_id, child_node_id, edge_type,
                   inference_source, inference_status, inference_confidence,
                   meta AS edge_meta
            FROM aios.semantic_topology_edge
            WHERE edge_id = ANY($1::uuid[])
            """,
            edge_ids,
        )
        current.update((("edge", row["edge_id"]), row) for row in rows)

    if anchor_ids:
        rows = await db.fetch(
            """
            SELECT anchor_edge_id, source_node_id, target_node_id,
                   relationship_type, target_scope_key, confidence,
                   inference_source, inference_status
            FROM aios.semantic_anchor_edge
            WHERE anchor_edge_id = ANY($1::uuid[])
            """,
            anchor_ids,
        )
        current.update((("anchor", row["anchor_edge_id"]), row) for row in rows)

    return current


def _current_delta_triples(
    scope_iri: str,
    *,
    kind: str,
    row: Any,
) -> list[str]:
    if kind == "node":
        return _node_triples(scope_iri, row)
    if kind == "edge":
        return _edge_triples(scope_iri, row)
    return _anchor_triples(scope_iri, row)


class _RdfDeltaBatchWriter:
    """Bound idempotent live-graph deltas by both bytes and changed objects."""

    def __init__(
        self,
        fuseki: Any,
        *,
        dataset: str,
        graph: str,
        target_bytes: int = RDF_DELTA_TARGET_BYTES,
        target_objects: int = RDF_DELTA_TARGET_OBJECTS,
    ):
        self.fuseki = fuseki
        self.dataset = dataset
        self.graph = graph
        self.target_bytes = target_bytes
        self.target_objects = target_objects
        self._operations: list[str] = []
        self._bytes = 0
        self._objects = 0
        self.batch_count = 0

    def add_object(self, *, deletes: list[str], triples: list[str]) -> None:
        operations = list(deletes)
        if triples:
            operations.append(
                f"INSERT DATA {{ GRAPH <{self.graph}> {{ "
                + " ".join(triples)
                + " } }"
            )
        payload = ";\n".join(operations)
        payload_bytes = len(payload.encode("utf-8")) + 2
        if self._operations and (
            self._bytes + payload_bytes > self.target_bytes
            or self._objects >= self.target_objects
        ):
            self.flush()
        self._operations.append(payload)
        self._bytes += payload_bytes
        self._objects += 1
        if self._bytes >= self.target_bytes or self._objects >= self.target_objects:
            self.flush()

    def flush(self) -> None:
        if not self._operations:
            return
        self.fuseki.update(self.dataset, ";\n".join(self._operations))
        self.batch_count += 1
        self._operations.clear()
        self._bytes = 0
        self._objects = 0


async def _project_scope_rdf_delta(
    db: Database,
    fuseki: Any,
    *,
    decision: Any,
    from_change_id: int,
    target_change_id: int,
) -> tuple[str, str, int]:
    """Apply a bounded, replay-safe PostgreSQL mutation window to the live graph."""

    dataset, graph = _TOPOLOGY_MODULE._rdf_graph(decision)
    scope_iri = _scope_iri(decision.scope_key)
    changes = await db.fetch(
        """
        SELECT change_id, object_kind, object_id, operation, old_state
        FROM aios.semantic_rdf_change
        WHERE scope_key=$1
          AND change_id > $2
          AND change_id <= $3
        ORDER BY change_id
        """,
        decision.scope_key,
        from_change_id,
        target_change_id,
    )

    if not changes:
        return dataset, graph, 0

    # Preserve every historical deletion coordinate in the cursor window while
    # inserting only the final authoritative PostgreSQL representation.
    object_ids: dict[str, set[Any]] = {"node": set(), "edge": set(), "anchor": set()}
    deletes: dict[tuple[str, Any], list[str]] = {}
    ordered_keys: list[tuple[str, Any]] = []
    for change in changes:
        kind = str(change["object_kind"])
        key = (kind, change["object_id"])
        object_ids[kind].add(change["object_id"])
        if key not in deletes:
            deletes[key] = []
            ordered_keys.append(key)
        statement = _delete_change_sparql(graph, scope_iri, change)
        if statement not in deletes[key]:
            deletes[key].append(statement)

    current = await _fetch_current_delta_rows(db, object_ids=object_ids)
    writer = _RdfDeltaBatchWriter(fuseki, dataset=dataset, graph=graph)
    for kind, object_id in ordered_keys:
        row = current.get((kind, object_id))
        triples = (
            _current_delta_triples(scope_iri, kind=kind, row=row)
            if row is not None
            else []
        )
        writer.add_object(deletes=deletes[(kind, object_id)], triples=triples)
    writer.flush()

    changed = len(ordered_keys)
    logger.info(
        "Projected RDF delta scope=%s dataset=%s changes=%s events=%s batches=%s cursor=%s..%s",
        decision.scope_key,
        dataset,
        changed,
        len(changes),
        writer.batch_count,
        from_change_id,
        target_change_id,
    )
    return dataset, graph, changed

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
    scope_iri = _scope_iri(decision.scope_key)

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

        fuseki.update(
            dataset,
            (
                f"MOVE SILENT GRAPH <{staging_graph}> TO GRAPH <{graph}>"
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
    """Project the latest authoritative PostgreSQL topology for one quiet scope."""

    if _REAL_PROJECT_SCOPE_RDF is None or _TOPOLOGY_MODULE is None:
        raise RuntimeError("semantic topology projection boundary is not installed")

    state = await db.fetchrow(
        """
        SELECT scope_key, scope_kind, rdf_dataset, rdf_graph,
               dirty_version, projected_version, dirty_at,
               rdf_change_cursor, rdf_delta_ready,
               COALESCE((
                   SELECT max(change_id)
                   FROM aios.semantic_rdf_change c
                   WHERE c.scope_key=semantic_scope_projection_state.scope_key
               ), rdf_change_cursor) AS target_change_id,
               (
                    dirty_at IS NULL
                    OR dirty_at <= now() - make_interval(secs => $2)
               ) AS quiet_ready
        FROM aios.semantic_scope_projection_state
        WHERE scope_key=$1
        """,
        scope_key,
        RDF_PROJECTION_QUIET_SECONDS,
    )
    if not state:
        return {"scope_key": scope_key, "projected": False, "reason": "not_dirty"}

    target_version = int(state["dirty_version"] or 0)
    if target_version <= int(state["projected_version"] or 0):
        return {"scope_key": scope_key, "projected": False, "reason": "already_current"}
    if not bool(state["quiet_ready"]):
        logger.debug(
            "Deferred hot RDF scope=%s version=%s dirty_at=%s",
            scope_key,
            target_version,
            state["dirty_at"],
        )
        return {"scope_key": scope_key, "projected": False, "reason": "debouncing"}

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
        # The final topology node may itself have been deleted. PostgreSQL is
        # authoritative, so an empty scope must retract any old live RDF rather
        # than merely advancing the ledger.
        if state["rdf_dataset"] and state["rdf_graph"]:
            fuseki.update(
                str(state["rdf_dataset"]),
                f"CLEAR SILENT GRAPH <{state['rdf_graph']}>",
            )
        await db.execute(
            """
            UPDATE aios.semantic_scope_projection_state
            SET projected_version=dirty_version,
                rdf_change_cursor=GREATEST(rdf_change_cursor,$2),
                rdf_delta_ready=true,
                status='ready', projected_at=now(), last_error=NULL,
                updated_at=now()
            WHERE scope_key=$1
            """,
            scope_key,
            int(state["target_change_id"] or 0),
        )
        await db.execute(
            "DELETE FROM aios.semantic_rdf_change WHERE scope_key=$1 AND change_id <= $2",
            scope_key,
            int(state["target_change_id"] or 0),
        )
        return {"scope_key": scope_key, "projected": True, "reason": "empty_scope_cleared"}

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

    target_change_id = int(state["target_change_id"] or 0)
    delta_ready = bool(state["rdf_delta_ready"])
    try:
        if delta_ready:
            dataset, graph, _ = await _project_scope_rdf_delta(
                db,
                fuseki,
                decision=decision,
                from_change_id=int(state["rdf_change_cursor"] or 0),
                target_change_id=target_change_id,
            )
        else:
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
            rdf_change_cursor=GREATEST(rdf_change_cursor,$5),
            rdf_delta_ready=true,
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
        target_change_id,
    )

    await db.execute(
        "DELETE FROM aios.semantic_rdf_change WHERE scope_key=$1 AND change_id <= $2",
        scope_key,
        target_change_id,
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
    """Enqueue only dirty scopes that have remained quiet long enough to publish."""

    rows = await db.fetch(
        """
        SELECT s.scope_key
        FROM aios.semantic_scope_projection_state s
        WHERE s.dirty_version > s.projected_version
          AND (
                s.dirty_at IS NULL
                OR s.dirty_at <= now() - make_interval(secs => $2)
          )
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
        RDF_PROJECTION_QUIET_SECONDS,
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
            priority=RDF_PROJECTION_PRIORITY,
        )
        created += int(job_id is not None)
    return created
