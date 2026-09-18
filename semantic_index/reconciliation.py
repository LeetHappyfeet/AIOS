from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.topology import (
    TopologyDecision,
    _upsert_edge,
    _upsert_node,
    reproject_existing_scope,
)
from aios_app.rdf.fuseki import FusekiClient
from .classifier import CLASSIFIER_VERSION
from .config import SemanticIndexConfig
from .neighbor_classifier import NEIGHBOR_CLASSIFIER_VERSION

logger = logging.getLogger("aios.semantic_reconciliation")


def _json_object(value: Any) -> dict[str, Any]:
    """Normalize JSON/JSONB values returned by asyncpg into a Python dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if decoded is None:
            return {}
        if not isinstance(decoded, dict):
            raise ValueError("expected JSON object metadata")
        return decoded
    try:
        return dict(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected object-like metadata") from exc


RECONCILER_VERSION = "semantic-reconciliation-v2"
EVENT_RESOLVER_VERSION = "semantic-event-resolver-v1"

PAIR_EDGE_TYPES = {
    "EQUIVALENT": "semantic_equivalent",
    "REFINES": "semantic_refinement",
    "CONTRADICTS": "semantic_contradicts",
    "SAME_TOPIC": "semantic_same_topic",
}

PIVOT_NODE_TYPES = {
    "EVENT_REGION": "EVENT",
    "STATE_SERIES": "STATE",
    "MEMORY_REGION": "MEMORY",
    "BELIEF_REGION": "CONCEPT",
    "RULE_REGION": "RULE",
    "GOAL_REGION": "GOAL",
    "TOPIC_REGION": "CONCEPT",
}

BOUNDARY_EDGE_TYPES = {
    "SAME_REGION": "semantic_region_bridge",
    "TOPIC_SPLIT": "topic_boundary",
    "TEMPORAL_TRANSITION": "temporal_transition",
    "STATE_TRANSITION": "state_transition",
    "NARRATIVE_SPLIT": "narrative_boundary",
    "CONTRADICTION_CLUSTER": "contradiction_boundary",
    "EXPERIENTIAL_BRANCH_CANDIDATE": "possible_experiential_branch",
    "WORLD_BRANCH_CANDIDATE": "possible_world_branch",
}


def _scope_partition(row: Any) -> str:
    if row["scope_kind"] == "character":
        instance_id = row["character_instance_id"]
        return f"{row['scope_key']}:instance:{instance_id or 'unresolved'}"
    return str(row["scope_key"])


def _decision_from_row(row: Any) -> TopologyDecision:
    return TopologyDecision(
        scope_kind=row["scope_kind"],
        scope_key=row["scope_key"],
        branch_kind="semantic_reconciliation",
        significance=0.7,
        character_id=row["character_id"],
        character_instance_id=row["character_instance_id"],
        world_id=row["world_id"],
        source_id=row["source_id"],
    )


def _pair_source_id(a: UUID, b: UUID, relation: str) -> str:
    left, right = sorted((str(a), str(b)))
    return f"{left}:{right}:{relation}:{NEIGHBOR_CLASSIFIER_VERSION}"


async def _record_receipt(
    db: Database,
    *,
    receipt_key: str,
    source_kind: str,
    source_id: str,
    scope_key: str,
    scope_partition_key: str,
    action: str,
    topology_node_id: UUID | None = None,
    topology_edge_id: UUID | None = None,
    rdf_dataset: str | None = None,
    rdf_graph: str | None = None,
    classifier_version: str | None = None,
    confidence: float | None = None,
    status: str = "accepted",
    meta: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO aios.semantic_reconciliation_receipt (
            receipt_key, source_kind, source_id, scope_key, scope_partition_key, action,
            topology_node_id, topology_edge_id, rdf_dataset, rdf_graph,
            classifier_version, confidence, status, meta,
            reconciled_at, updated_at
        )
        VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,now(),now()
        )
        ON CONFLICT (receipt_key) DO UPDATE
        SET topology_node_id=COALESCE(EXCLUDED.topology_node_id, aios.semantic_reconciliation_receipt.topology_node_id),
            topology_edge_id=COALESCE(EXCLUDED.topology_edge_id, aios.semantic_reconciliation_receipt.topology_edge_id),
            rdf_dataset=COALESCE(EXCLUDED.rdf_dataset, aios.semantic_reconciliation_receipt.rdf_dataset),
            rdf_graph=COALESCE(EXCLUDED.rdf_graph, aios.semantic_reconciliation_receipt.rdf_graph),
            confidence=GREATEST(
                COALESCE(aios.semantic_reconciliation_receipt.confidence,0),
                COALESCE(EXCLUDED.confidence,0)
            ),
            status=EXCLUDED.status,
            meta=aios.semantic_reconciliation_receipt.meta || EXCLUDED.meta,
            updated_at=now()
        """,
        receipt_key,
        source_kind,
        source_id,
        scope_key,
        scope_partition_key,
        action,
        topology_node_id,
        topology_edge_id,
        rdf_dataset,
        rdf_graph,
        classifier_version,
        confidence,
        status,
        json.dumps(meta or {}),
    )


async def _preferred_scope_nodes(
    db: Database,
    *,
    proposition_a: UUID,
    proposition_b: UUID,
) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT
            a.scope_key,
            a.scope_kind,
            COALESCE(a.character_id,b.character_id) AS character_id,
            COALESCE(a.character_instance_id,b.character_instance_id) AS character_instance_id,
            COALESCE(a.world_id,b.world_id) AS world_id,
            COALESCE(a.source_id,b.source_id) AS source_id,
            a.topology_node_id AS a_node,
            b.topology_node_id AS b_node,
            a.proposition_id AS proposition_a,
            b.proposition_id AS proposition_b
        FROM aios.semantic_topology_node a
        JOIN aios.semantic_topology_node b
          ON b.scope_key=a.scope_key
         AND b.node_type='PROPOSITION'
         AND b.proposition_id=$2
         AND (
             a.scope_kind <> 'character'
             OR a.character_instance_id=b.character_instance_id
         )
        WHERE a.node_type='PROPOSITION'
          AND a.proposition_id=$1
          AND a.topology_node_id<>b.topology_node_id
        """,
        proposition_a,
        proposition_b,
    )
    return [dict(row) for row in rows]


async def _safe_reproject_scope(
    db: Database,
    fuseki: FusekiClient,
    *,
    scope_key: str,
) -> tuple[str, str] | None:
    try:
        return await reproject_existing_scope(
            db,
            fuseki,
            scope_key=scope_key,
        )
    except Exception as exc:
        logger.warning(
            "Semantic RDF projection failed for scope %s; will retry: %s",
            scope_key,
            exc,
        )
        return None


async def _semantic_event_for_proposition(db: Database, proposition_id: UUID) -> UUID | None:
    row = await db.fetchrow(
        """
        SELECT semantic_event_id
        FROM aios.semantic_event_membership
        WHERE proposition_id=$1 AND status='active'
        ORDER BY created_at
        LIMIT 1
        """,
        proposition_id,
    )
    return row["semantic_event_id"] if row else None


async def _semantic_event_evidence(db: Database, proposition_id: UUID) -> dict[str, Any]:
    row = await db.fetchrow(
        """
        SELECT observation_id, claim_id, timeline_id, dag_node_id
        FROM aios.observation
        WHERE proposition_id=$1
        ORDER BY observed_at
        LIMIT 1
        """,
        proposition_id,
    )
    return dict(row) if row else {}


async def _attach_semantic_event_member(
    db: Database,
    *,
    semantic_event_id: UUID,
    proposition_id: UUID,
    confidence: float,
    source_id: str,
) -> None:
    evidence = await _semantic_event_evidence(db, proposition_id)
    await db.execute(
        """
        INSERT INTO aios.semantic_event_membership (
            semantic_event_id, proposition_id, observation_id, claim_id,
            membership_confidence, assigned_by, evidence_source_id,
            status, meta, created_at, updated_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,'active','{}'::jsonb,now(),now())
        ON CONFLICT (semantic_event_id, proposition_id) DO UPDATE
        SET membership_confidence=GREATEST(
                aios.semantic_event_membership.membership_confidence,
                EXCLUDED.membership_confidence
            ),
            evidence_source_id=EXCLUDED.evidence_source_id,
            assigned_by=EXCLUDED.assigned_by,
            status='active',
            updated_at=now()
        """,
        semantic_event_id,
        proposition_id,
        evidence.get("observation_id"),
        evidence.get("claim_id"),
        confidence,
        EVENT_RESOLVER_VERSION,
        source_id,
    )


async def _resolve_semantic_event_pair(
    db: Database,
    *,
    proposition_a: UUID,
    proposition_b: UUID,
    confidence: float,
    source_id: str,
    world_id: UUID | None,
    evidence: dict[str, Any],
) -> tuple[UUID | None, str]:
    """Resolve accepted SAME_EVENT evidence without assuming transitive identity."""
    event_a = await _semantic_event_for_proposition(db, proposition_a)
    event_b = await _semantic_event_for_proposition(db, proposition_b)

    if event_a is not None and event_b is not None:
        if event_a == event_b:
            return event_a, "existing_semantic_event"

        left, right = sorted((event_a, event_b), key=str)
        await db.execute(
            """
            INSERT INTO aios.semantic_event_merge_candidate (
                event_a_id, event_b_id, source_id, confidence, status,
                resolver_version, evidence, created_at, updated_at
            )
            VALUES ($1,$2,$3,$4,'candidate',$5,$6::jsonb,now(),now())
            ON CONFLICT (event_a_id, event_b_id, source_id) DO UPDATE
            SET confidence=GREATEST(
                    aios.semantic_event_merge_candidate.confidence,
                    EXCLUDED.confidence
                ),
                evidence=aios.semantic_event_merge_candidate.evidence || EXCLUDED.evidence,
                updated_at=now()
            """,
            left, right, source_id, confidence, EVENT_RESOLVER_VERSION,
            json.dumps(evidence),
        )
        return None, "semantic_event_merge_candidate"

    semantic_event_id = event_a or event_b
    created = semantic_event_id is None
    if created:
        ev_a = await _semantic_event_evidence(db, proposition_a)
        ev_b = await _semantic_event_evidence(db, proposition_b)
        shared_timeline = (
            ev_a.get("timeline_id")
            if ev_a.get("timeline_id") is not None
            and ev_a.get("timeline_id") == ev_b.get("timeline_id")
            else None
        )
        shared_dag = (
            ev_a.get("dag_node_id")
            if ev_a.get("dag_node_id") is not None
            and ev_a.get("dag_node_id") == ev_b.get("dag_node_id")
            else None
        )
        left, right = sorted((str(proposition_a), str(proposition_b)))
        event_key = f"same-event:{NEIGHBOR_CLASSIFIER_VERSION}:{left}:{right}"
        row = await db.fetchrow(
            """
            INSERT INTO aios.semantic_event (
                event_key, world_id, timeline_id, dag_node_id, status,
                confidence, resolver_version, meta, created_at, updated_at
            )
            VALUES ($1,$2,$3,$4,'active',$5,$6,$7::jsonb,now(),now())
            ON CONFLICT (event_key) DO UPDATE
            SET confidence=GREATEST(aios.semantic_event.confidence, EXCLUDED.confidence),
                updated_at=now()
            RETURNING semantic_event_id
            """,
            event_key, world_id, shared_timeline, shared_dag, confidence,
            EVENT_RESOLVER_VERSION,
            json.dumps({
                "source": "semantic_neighbor_relation",
                "classifier_version": NEIGHBOR_CLASSIFIER_VERSION,
            }),
        )
        semantic_event_id = row["semantic_event_id"]

    await _attach_semantic_event_member(
        db, semantic_event_id=semantic_event_id, proposition_id=proposition_a,
        confidence=confidence, source_id=source_id,
    )
    await _attach_semantic_event_member(
        db, semantic_event_id=semantic_event_id, proposition_id=proposition_b,
        confidence=confidence, source_id=source_id,
    )
    await db.execute(
        """
        UPDATE aios.semantic_event
        SET confidence=GREATEST(confidence,$2), updated_at=now()
        WHERE semantic_event_id=$1
        """,
        semantic_event_id, confidence,
    )
    return semantic_event_id, (
        "create_semantic_event" if created else "attach_semantic_event_member"
    )


async def _materialize_semantic_event(
    db: Database,
    *,
    semantic_event_id: UUID,
    scope: dict[str, Any],
    confidence: float,
    source_id: str,
) -> UUID:
    event = await db.fetchrow(
        """
        SELECT timeline_id, dag_node_id
        FROM aios.semantic_event
        WHERE semantic_event_id=$1
        """,
        semantic_event_id,
    )
    decision = _decision_from_row(scope)
    event_node = await _upsert_node(
        db,
        decision=decision,
        node_type="EVENT",
        node_key=f"semantic_event:{semantic_event_id}:{_scope_partition(scope)}",
        label="SEMANTIC_EVENT",
        timeline_id=event["timeline_id"] if event else None,
        dag_node_id=event["dag_node_id"] if event else None,
        proposition_id=None,
        claim_id=None,
        assertion_id=None,
        significance=max(0.5, confidence),
        meta={
            "semantic_role": "canonical_event_identity",
            "semantic_event_id": str(semantic_event_id),
            "reconciler_version": RECONCILER_VERSION,
            "resolver_version": EVENT_RESOLVER_VERSION,
        },
    )

    structural_parent = await _semantic_pivot_parent(db, scope=scope)
    if structural_parent is not None:
        await _upsert_edge(
            db, decision=decision, parent=structural_parent, child=event_node,
            edge_type="contains_semantic_event",
            significance=max(0.5, confidence),
            inference_source="semantic_event_resolver",
            inference_status="accepted",
            inference_confidence=confidence,
            meta={"semantic_event_id": str(semantic_event_id)},
        )

    for proposition_id, member_node in (
        (scope["proposition_a"], scope["a_node"]),
        (scope["proposition_b"], scope["b_node"]),
    ):
        await _upsert_edge(
            db, decision=decision, parent=event_node, child=member_node,
            edge_type="semantic_event_evidence",
            significance=max(0.5, confidence),
            inference_source="semantic_event_resolver",
            inference_status="accepted",
            inference_confidence=confidence,
            meta={
                "semantic_event_id": str(semantic_event_id),
                "proposition_id": str(proposition_id),
                "source_id": source_id,
            },
        )
    return event_node


async def reconcile_neighbor_relations_once(
    db: Database,
    fuseki: FusekiClient,
    cfg: SemanticIndexConfig,
) -> int:
    rows = await db.fetch(
        """
        SELECT
            r.proposition_id,
            r.neighbor_proposition_id,
            r.relation,
            r.confidence,
            r.features,
            r.evidence
        FROM aios.semantic_neighbor_relation r
        WHERE r.embedding_version=$1
          AND r.classifier_version=$2
          AND r.status='candidate'
          AND r.confidence >= $3
          AND r.relation = ANY($4::text[])
          AND EXISTS (
              SELECT 1
              FROM aios.semantic_topology_node a
              JOIN aios.semantic_topology_node b
                ON b.scope_key=a.scope_key
               AND b.node_type='PROPOSITION'
               AND b.proposition_id=r.neighbor_proposition_id
               AND (
                   a.scope_kind <> 'character'
                   OR a.character_instance_id=b.character_instance_id
               )
              WHERE a.node_type='PROPOSITION'
                AND a.proposition_id=r.proposition_id
                AND a.topology_node_id<>b.topology_node_id
          )
        ORDER BY r.confidence DESC, r.created_at
        LIMIT $5
        """,
        cfg.embedding_version,
        NEIGHBOR_CLASSIFIER_VERSION,
        cfg.reconcile_relation_min_confidence,
        list(PAIR_EDGE_TYPES) + ["SAME_EVENT"],
        cfg.batch_size,
    )
    written = 0
    affected_scopes: set[str] = set()

    for row in rows:
        source_id = _pair_source_id(
            row["proposition_id"],
            row["neighbor_proposition_id"],
            row["relation"],
        )
        scopes = await _preferred_scope_nodes(
            db,
            proposition_a=row["proposition_id"],
            proposition_b=row["neighbor_proposition_id"],
        )

        for scope in scopes:
            partition_key = _scope_partition(scope)
            receipt_key = f"neighbor:{source_id}:{partition_key}"
            exists = await db.fetchrow(
                "SELECT 1 FROM aios.semantic_reconciliation_receipt WHERE receipt_key=$1",
                receipt_key,
            )
            if exists:
                continue

            if row["relation"] == "SAME_EVENT":
                semantic_event_id, action = await _resolve_semantic_event_pair(
                    db,
                    proposition_a=row["proposition_id"],
                    proposition_b=row["neighbor_proposition_id"],
                    confidence=float(row["confidence"]),
                    source_id=source_id,
                    world_id=scope.get("world_id"),
                    evidence={
                        "features": _json_object(row["features"]),
                        "classifier_evidence": _json_object(row["evidence"]),
                    },
                )
                event_node = None
                if semantic_event_id is not None:
                    event_node = await _materialize_semantic_event(
                        db,
                        semantic_event_id=semantic_event_id,
                        scope=scope,
                        confidence=float(row["confidence"]),
                        source_id=source_id,
                    )
                await _record_receipt(
                    db,
                    receipt_key=receipt_key,
                    source_kind="neighbor_relation",
                    source_id=source_id,
                    scope_key=scope["scope_key"],
                    scope_partition_key=partition_key,
                    action=action,
                    topology_node_id=event_node,
                    classifier_version=NEIGHBOR_CLASSIFIER_VERSION,
                    confidence=float(row["confidence"]),
                    status=("candidate" if semantic_event_id is None else "accepted"),
                    meta={
                        "relation": "SAME_EVENT",
                        "semantic_event_id": (
                            str(semantic_event_id) if semantic_event_id else None
                        ),
                        "resolver_version": EVENT_RESOLVER_VERSION,
                    },
                )
            else:
                decision = _decision_from_row(scope)
                parent = scope["a_node"]
                child = scope["b_node"]
                if str(parent) > str(child) and row["relation"] != "REFINES":
                    parent, child = child, parent

                edge_id = await _upsert_edge(
                    db,
                    decision=decision,
                    parent=parent,
                    child=child,
                    edge_type=PAIR_EDGE_TYPES[row["relation"]],
                    significance=max(0.5, float(row["confidence"])),
                    inference_source="semantic_vector_classifier",
                    inference_status="accepted",
                    inference_confidence=float(row["confidence"]),
                    meta={
                        "reconciler_version": RECONCILER_VERSION,
                        "classifier_version": NEIGHBOR_CLASSIFIER_VERSION,
                        "relation": row["relation"],
                        "features": _json_object(row["features"]),
                    },
                )
                await _record_receipt(
                    db,
                    receipt_key=receipt_key,
                    source_kind="neighbor_relation",
                    source_id=source_id,
                    scope_key=scope["scope_key"],
                    scope_partition_key=partition_key,
                    action=PAIR_EDGE_TYPES[row["relation"]],
                    topology_edge_id=edge_id,
                    classifier_version=NEIGHBOR_CLASSIFIER_VERSION,
                    confidence=float(row["confidence"]),
                    meta={"relation": row["relation"]},
                )
            affected_scopes.add(scope["scope_key"])
            written += 1

        await db.execute(
            """
            UPDATE aios.semantic_neighbor_relation
            SET status=CASE WHEN $5::integer > 0 THEN 'reconciled' ELSE status END,
                updated_at=now()
            WHERE proposition_id=$1
              AND neighbor_proposition_id=$2
              AND embedding_version=$3
              AND classifier_version=$4
            """,
            row["proposition_id"],
            row["neighbor_proposition_id"],
            cfg.embedding_version,
            NEIGHBOR_CLASSIFIER_VERSION,
            len(scopes),
        )

    for scope_key in affected_scopes:
        projected = await _safe_reproject_scope(db, fuseki, scope_key=scope_key)
        if not projected:
            continue
        dataset, graph = projected
        await db.execute(
            """
            UPDATE aios.semantic_reconciliation_receipt
            SET rdf_dataset=$2, rdf_graph=$3, updated_at=now()
            WHERE scope_key=$1
              AND rdf_dataset IS NULL
            """,
            scope_key,
            dataset,
            graph,
        )

    return written


async def _cluster_scope_rows(
    db: Database,
    *,
    cluster_id: UUID,
) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT
            n.scope_key,
            MIN(n.scope_kind) AS scope_kind,
            MIN(n.character_id) AS character_id,
            n.character_instance_id,
            MIN(n.world_id::text)::uuid AS world_id,
            MIN(n.source_id) AS source_id,
            array_agg(n.topology_node_id ORDER BY n.topology_node_id) AS member_nodes,
            COUNT(DISTINCT n.proposition_id) AS member_count
        FROM aios.semantic_topology_node n
        JOIN aios.semantic_cluster_membership m
          ON m.proposition_id=n.proposition_id
        WHERE m.cluster_id=$1
          AND n.node_type='PROPOSITION'
        GROUP BY n.scope_key, n.character_instance_id
        HAVING COUNT(DISTINCT n.proposition_id) >= 2
        """,
        cluster_id,
    )
    return [dict(row) for row in rows]


async def _semantic_pivot_parent(
    db: Database,
    *,
    scope: dict[str, Any],
) -> UUID | None:
    if scope["scope_kind"] == "character" and scope.get("character_instance_id"):
        row = await db.fetchrow(
            """
            SELECT topology_node_id
            FROM aios.semantic_topology_node
            WHERE scope_key=$1
              AND node_type='INSTANCE'
              AND node_key=$2
            ORDER BY significance DESC, created_at
            LIMIT 1
            """,
            scope["scope_key"],
            str(scope["character_instance_id"]),
        )
        if row:
            return row["topology_node_id"]

    row = await db.fetchrow(
        """
        SELECT topology_node_id
        FROM aios.semantic_topology_node
        WHERE scope_key=$1
          AND node_type='ROOT'
        ORDER BY significance DESC, created_at
        LIMIT 1
        """,
        scope["scope_key"],
    )
    return row["topology_node_id"] if row else None


async def _retract_superseded_cluster_topology(
    db: Database,
) -> set[str]:
    """Remove materialized cluster pivots whose source cluster is no longer current.

    Clustering runs are historical snapshots, but semantic topology represents the
    current accepted structure.  A superseded cluster candidate must therefore
    stop contributing its pivot and membership/boundary edges.  Deleting the
    derived pivot is sufficient because topology edges reference nodes with
    ON DELETE CASCADE; reconciliation receipts retain history via ON DELETE SET NULL.
    """
    rows = await db.fetch(
        """
        DELETE FROM aios.semantic_topology_node n
        USING aios.semantic_reconciliation_receipt r,
              aios.semantic_cluster_classification cc,
              aios.semantic_cluster_candidate c
        WHERE r.source_kind='cluster'
          AND r.topology_node_id=n.topology_node_id
          AND cc.classification_id::text=r.source_id
          AND c.cluster_id=cc.cluster_id
          AND c.status='stale'
          AND n.node_key LIKE 'cluster:%'
          AND n.meta->>'reconciler_version'=$1
        RETURNING n.scope_key
        """,
        RECONCILER_VERSION,
    )
    return {str(row["scope_key"]) for row in rows}


async def reconcile_clusters_once(
    db: Database,
    fuseki: FusekiClient,
    cfg: SemanticIndexConfig,
) -> int:
    rows = await db.fetch(
        """
        SELECT
            cc.classification_id,
            cc.cluster_id,
            cc.classification,
            cc.confidence,
            cc.feature_scores,
            cc.evidence,
            c.cluster_key,
            c.member_count,
            c.meta
        FROM aios.semantic_cluster_classification cc
        JOIN aios.semantic_cluster_candidate c ON c.cluster_id=cc.cluster_id
        WHERE cc.classifier_version=$1
          AND cc.status='candidate'
          AND cc.confidence >= $2
          AND cc.classification <> 'UNRESOLVED'
        ORDER BY cc.confidence DESC, cc.created_at
        LIMIT $3
        """,
        CLASSIFIER_VERSION,
        cfg.reconcile_cluster_min_confidence,
        cfg.batch_size,
    )
    written = 0
    affected_scopes = await _retract_superseded_cluster_topology(db)

    for row in rows:
        scopes = await _cluster_scope_rows(db, cluster_id=row["cluster_id"])
        for scope in scopes:
            source_id = str(row["classification_id"])
            partition_key = _scope_partition(scope)
            receipt_key = f"cluster:{source_id}:{partition_key}"
            exists = await db.fetchrow(
                "SELECT 1 FROM aios.semantic_reconciliation_receipt WHERE receipt_key=$1",
                receipt_key,
            )
            if exists:
                continue

            decision = _decision_from_row(scope)
            pivot_node_type = PIVOT_NODE_TYPES.get(
                row["classification"],
                "SEMANTIC_CLUSTER",
            )
            cluster_node = await _upsert_node(
                db,
                decision=decision,
                node_type=pivot_node_type,
                node_key=f"cluster:{row['cluster_key']}:{partition_key}",
                label=row["classification"],
                timeline_id=None,
                dag_node_id=None,
                proposition_id=None,
                claim_id=None,
                assertion_id=None,
                significance=max(0.5, float(row["confidence"])),
                meta={
                    "reconciler_version": RECONCILER_VERSION,
                    "classification": row["classification"],
                    "classifier_version": CLASSIFIER_VERSION,
                    "cluster_id": str(row["cluster_id"]),
                    "cluster_key": str(row["cluster_key"]),
                    "member_count": int(row["member_count"]),
                    "semantic_pivot": pivot_node_type != "SEMANTIC_CLUSTER",
                },
            )

            structural_parent = await _semantic_pivot_parent(
                db,
                scope=scope,
            )
            if structural_parent is not None:
                await _upsert_edge(
                    db,
                    decision=decision,
                    parent=structural_parent,
                    child=cluster_node,
                    edge_type=(
                        "contains_semantic_pivot"
                        if pivot_node_type != "SEMANTIC_CLUSTER"
                        else "contains_semantic_cluster"
                    ),
                    significance=max(0.5, float(row["confidence"])),
                    inference_source="semantic_cluster_classifier",
                    inference_status="accepted",
                    inference_confidence=float(row["confidence"]),
                    meta={
                        "classification": row["classification"],
                        "cluster_id": str(row["cluster_id"]),
                    },
                )

            for member_node in scope["member_nodes"]:
                await _upsert_edge(
                    db,
                    decision=decision,
                    parent=cluster_node,
                    child=member_node,
                    edge_type=(
                        "semantic_pivot_member"
                        if pivot_node_type != "SEMANTIC_CLUSTER"
                        else "semantic_cluster_member"
                    ),
                    significance=max(0.45, float(row["confidence"])),
                    inference_source="semantic_cluster_classifier",
                    inference_status="accepted",
                    inference_confidence=float(row["confidence"]),
                    meta={
                        "classification": row["classification"],
                        "cluster_id": str(row["cluster_id"]),
                    },
                )

            await _record_receipt(
                db,
                receipt_key=receipt_key,
                source_kind="cluster",
                source_id=source_id,
                scope_key=scope["scope_key"],
                scope_partition_key=partition_key,
                action=(
                    "materialize_semantic_pivot"
                    if pivot_node_type != "SEMANTIC_CLUSTER"
                    else "materialize_semantic_cluster"
                ),
                topology_node_id=cluster_node,
                classifier_version=CLASSIFIER_VERSION,
                confidence=float(row["confidence"]),
                meta={"classification": row["classification"]},
            )
            affected_scopes.add(scope["scope_key"])
            written += 1

        if scopes:
            await db.execute(
                """
                UPDATE aios.semantic_cluster_classification
                SET status='reconciled', updated_at=now()
                WHERE classification_id=$1
                """,
                row["classification_id"],
            )

    for scope_key in affected_scopes:
        projected = await _safe_reproject_scope(db, fuseki, scope_key=scope_key)
        if projected:
            dataset, graph = projected
            await db.execute(
                """
                UPDATE aios.semantic_reconciliation_receipt
                SET rdf_dataset=$2, rdf_graph=$3, updated_at=now()
                WHERE scope_key=$1
                  AND rdf_dataset IS NULL
                """,
                scope_key,
                dataset,
                graph,
            )

    return written


async def _cluster_node_for_scope(
    db: Database,
    *,
    scope_partition_key: str,
    cluster_id: UUID,
) -> UUID | None:
    row = await db.fetchrow(
        """
        SELECT r.topology_node_id
        FROM aios.semantic_reconciliation_receipt r
        JOIN aios.semantic_cluster_classification cc
          ON cc.classification_id::text=r.source_id
        WHERE r.source_kind='cluster'
          AND r.scope_partition_key=$1
          AND cc.cluster_id=$2
          AND r.topology_node_id IS NOT NULL
        ORDER BY r.reconciled_at DESC
        LIMIT 1
        """,
        scope_partition_key,
        cluster_id,
    )
    return row["topology_node_id"] if row else None


async def _branch_candidate(
    db: Database,
    *,
    classification_id: UUID,
    run_id: UUID,
    scope: dict[str, Any],
    cluster_a_id: UUID,
    cluster_b_id: UUID,
    classification: str,
    confidence: float,
    evidence: dict[str, Any],
) -> None:
    candidate_kind = (
        "experiential"
        if classification == "EXPERIENTIAL_BRANCH_CANDIDATE"
        else "world"
    )
    if candidate_kind == "experiential" and scope["scope_kind"] != "character":
        return
    await db.execute(
        """
        INSERT INTO aios.semantic_branch_candidate (
            boundary_classification_id, run_id, scope_key, scope_partition_key, scope_kind,
            candidate_kind, cluster_a_id, cluster_b_id,
            character_id, character_instance_id, world_id, timeline_id,
            confidence, status, reason, created_at, updated_at
        )
        VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
            'candidate',$14::jsonb,now(),now()
        )
        ON CONFLICT (boundary_classification_id, scope_partition_key, candidate_kind)
        DO UPDATE SET
            confidence=GREATEST(aios.semantic_branch_candidate.confidence, EXCLUDED.confidence),
            reason=aios.semantic_branch_candidate.reason || EXCLUDED.reason,
            updated_at=now()
        """,
        classification_id,
        run_id,
        scope["scope_key"],
        _scope_partition(scope),
        scope["scope_kind"],
        candidate_kind,
        cluster_a_id,
        cluster_b_id,
        scope.get("character_id"),
        scope.get("character_instance_id"),
        scope.get("world_id"),
        None,
        confidence,
        json.dumps({
            "reconciler_version": RECONCILER_VERSION,
            "classification": classification,
            "evidence": evidence,
        }),
    )


async def reconcile_boundaries_once(
    db: Database,
    fuseki: FusekiClient,
    cfg: SemanticIndexConfig,
) -> int:
    rows = await db.fetch(
        """
        SELECT
            bc.classification_id,
            bc.run_id,
            bc.cluster_a_id,
            bc.cluster_b_id,
            bc.classification,
            bc.confidence,
            bc.feature_scores,
            bc.evidence
        FROM aios.semantic_boundary_classification bc
        WHERE bc.classifier_version=$1
          AND bc.status='candidate'
          AND bc.confidence >= $2
          AND bc.classification <> 'UNRESOLVED'
        ORDER BY bc.confidence DESC, bc.created_at
        LIMIT $3
        """,
        CLASSIFIER_VERSION,
        cfg.reconcile_boundary_min_confidence,
        cfg.batch_size,
    )
    written = 0
    affected_scopes: set[str] = set()

    for row in rows:
        scopes = await db.fetch(
            """
            SELECT DISTINCT
                ra.scope_key,
                ra.scope_partition_key,
                na.scope_kind,
                na.character_id,
                na.character_instance_id,
                na.world_id,
                na.source_id
            FROM aios.semantic_reconciliation_receipt ra
            JOIN aios.semantic_cluster_classification cca
              ON cca.classification_id::text=ra.source_id
            JOIN aios.semantic_reconciliation_receipt rb
              ON rb.scope_partition_key=ra.scope_partition_key
             AND rb.source_kind='cluster'
            JOIN aios.semantic_cluster_classification ccb
              ON ccb.classification_id::text=rb.source_id
            JOIN aios.semantic_topology_node na
              ON na.topology_node_id=ra.topology_node_id
            WHERE ra.source_kind='cluster'
              AND cca.cluster_id=$1
              AND ccb.cluster_id=$2
            """,
            row["cluster_a_id"],
            row["cluster_b_id"],
        )

        for raw_scope in scopes:
            scope = dict(raw_scope)
            node_a = await _cluster_node_for_scope(
                db,
                scope_partition_key=scope["scope_partition_key"],
                cluster_id=row["cluster_a_id"],
            )
            node_b = await _cluster_node_for_scope(
                db,
                scope_partition_key=scope["scope_partition_key"],
                cluster_id=row["cluster_b_id"],
            )
            if node_a is None or node_b is None or node_a == node_b:
                continue

            edge_type = BOUNDARY_EDGE_TYPES[row["classification"]]
            partition_key = scope["scope_partition_key"]
            receipt_key = f"boundary:{row['classification_id']}:{partition_key}"
            exists = await db.fetchrow(
                "SELECT 1 FROM aios.semantic_reconciliation_receipt WHERE receipt_key=$1",
                receipt_key,
            )
            if exists:
                continue

            parent, child = node_a, node_b
            if str(parent) > str(child) and row["classification"] not in {
                "TEMPORAL_TRANSITION",
                "STATE_TRANSITION",
            }:
                parent, child = child, parent

            decision = _decision_from_row(scope)
            edge_id = await _upsert_edge(
                db,
                decision=decision,
                parent=parent,
                child=child,
                edge_type=edge_type,
                significance=max(0.5, float(row["confidence"])),
                inference_source="semantic_boundary_classifier",
                inference_status=(
                    "candidate"
                    if "BRANCH_CANDIDATE" in row["classification"]
                    else "accepted"
                ),
                inference_confidence=float(row["confidence"]),
                meta={
                    "reconciler_version": RECONCILER_VERSION,
                    "classification": row["classification"],
                    "classifier_version": CLASSIFIER_VERSION,
                    "feature_scores": _json_object(row["feature_scores"]),
                },
            )

            await _record_receipt(
                db,
                receipt_key=receipt_key,
                source_kind="boundary",
                source_id=str(row["classification_id"]),
                scope_key=scope["scope_key"],
                scope_partition_key=partition_key,
                action=edge_type,
                topology_edge_id=edge_id,
                classifier_version=CLASSIFIER_VERSION,
                confidence=float(row["confidence"]),
                status=(
                    "candidate"
                    if "BRANCH_CANDIDATE" in row["classification"]
                    else "accepted"
                ),
                meta={"classification": row["classification"]},
            )

            if row["classification"] in {
                "EXPERIENTIAL_BRANCH_CANDIDATE",
                "WORLD_BRANCH_CANDIDATE",
            }:
                await _branch_candidate(
                    db,
                    classification_id=row["classification_id"],
                    run_id=row["run_id"],
                    scope=scope,
                    cluster_a_id=row["cluster_a_id"],
                    cluster_b_id=row["cluster_b_id"],
                    classification=row["classification"],
                    confidence=float(row["confidence"]),
                    evidence=_json_object(row["evidence"]),
                )

            affected_scopes.add(scope["scope_key"])
            written += 1

        if scopes:
            await db.execute(
                """
                UPDATE aios.semantic_boundary_classification
                SET status='reconciled', updated_at=now()
                WHERE classification_id=$1
                """,
                row["classification_id"],
            )

    for scope_key in affected_scopes:
        projected = await _safe_reproject_scope(db, fuseki, scope_key=scope_key)
        if projected:
            dataset, graph = projected
            await db.execute(
                """
                UPDATE aios.semantic_reconciliation_receipt
                SET rdf_dataset=$2, rdf_graph=$3, updated_at=now()
                WHERE scope_key=$1
                  AND rdf_dataset IS NULL
                """,
                scope_key,
                dataset,
                graph,
            )

    return written


async def reproject_pending_reconciliation_once(
    db: Database,
    fuseki: FusekiClient,
    cfg: SemanticIndexConfig,
) -> int:
    rows = await db.fetch(
        """
        SELECT DISTINCT scope_key
        FROM aios.semantic_reconciliation_receipt
        WHERE rdf_dataset IS NULL
        ORDER BY scope_key
        LIMIT $1
        """,
        cfg.batch_size,
    )
    projected_count = 0
    for row in rows:
        scope_key = row["scope_key"]
        try:
            projected = await _safe_reproject_scope(
                db,
                fuseki,
                scope_key=scope_key,
            )
            if not projected:
                continue
            dataset, graph = projected
            await db.execute(
                """
                UPDATE aios.semantic_reconciliation_receipt
                SET rdf_dataset=$2,
                    rdf_graph=$3,
                    updated_at=now()
                WHERE scope_key=$1
                  AND rdf_dataset IS NULL
                """,
                scope_key,
                dataset,
                graph,
            )
            projected_count += 1
        except Exception as exc:
            logger.warning(
                "Semantic RDF catch-up failed for scope %s: %s",
                scope_key,
                exc,
            )
    return projected_count


async def reconcile_semantic_structure_once(
    db: Database,
    fuseki: FusekiClient,
    cfg: SemanticIndexConfig,
) -> int:
    pair_count = await reconcile_neighbor_relations_once(db, fuseki, cfg)
    cluster_count = await reconcile_clusters_once(db, fuseki, cfg)
    boundary_count = await reconcile_boundaries_once(db, fuseki, cfg)
    rdf_count = await reproject_pending_reconciliation_once(db, fuseki, cfg)
    total = pair_count + cluster_count + boundary_count + rdf_count
    if total:
        logger.info(
            "Semantic reconciliation promoted %d pair relations, %d clusters, "
            "and %d boundaries; reprojected %d RDF scopes",
            pair_count,
            cluster_count,
            boundary_count,
            rdf_count,
        )
    return total
