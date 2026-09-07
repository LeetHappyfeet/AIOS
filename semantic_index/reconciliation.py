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

RECONCILER_VERSION = "semantic-reconciliation-v1"

PAIR_EDGE_TYPES = {
    "EQUIVALENT": "semantic_equivalent",
    "REFINES": "semantic_refinement",
    "CONTRADICTS": "semantic_contradicts",
    "SAME_TOPIC": "semantic_same_topic",
    "SAME_EVENT": "semantic_same_event",
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
            receipt_key, source_kind, source_id, scope_key, action,
            topology_node_id, topology_edge_id, rdf_dataset, rdf_graph,
            classifier_version, confidence, status, meta,
            reconciled_at, updated_at
        )
        VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,now(),now()
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
        WITH ranked AS (
            SELECT
                n.*,
                row_number() OVER (
                    PARTITION BY n.scope_key, n.proposition_id
                    ORDER BY
                        CASE n.node_type
                            WHEN 'TOPIC' THEN 0
                            WHEN 'WORLD_ASSERTION' THEN 1
                            WHEN 'ACQUISITION' THEN 1
                            ELSE 2
                        END,
                        n.significance DESC,
                        n.created_at
                ) AS rn
            FROM aios.semantic_topology_node n
            WHERE n.proposition_id = ANY($1::uuid[])
        ),
        chosen AS (
            SELECT * FROM ranked WHERE rn=1
        )
        SELECT
            a.scope_key, a.scope_kind,
            COALESCE(a.character_id,b.character_id) AS character_id,
            COALESCE(a.character_instance_id,b.character_instance_id) AS character_instance_id,
            COALESCE(a.world_id,b.world_id) AS world_id,
            COALESCE(a.source_id,b.source_id) AS source_id,
            a.topology_node_id AS a_node,
            b.topology_node_id AS b_node
        FROM chosen a
        JOIN chosen b
          ON b.scope_key=a.scope_key
         AND b.proposition_id=$3
        WHERE a.proposition_id=$2
          AND a.topology_node_id<>b.topology_node_id
        """,
        [proposition_a, proposition_b],
        proposition_a,
        proposition_b,
    )
    return [dict(row) for row in rows]


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
        ORDER BY r.confidence DESC, r.created_at
        LIMIT $5
        """,
        cfg.embedding_version,
        NEIGHBOR_CLASSIFIER_VERSION,
        cfg.reconcile_relation_min_confidence,
        list(PAIR_EDGE_TYPES),
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
            receipt_key = f"neighbor:{source_id}:{scope['scope_key']}"
            exists = await db.fetchrow(
                "SELECT 1 FROM aios.semantic_reconciliation_receipt WHERE receipt_key=$1",
                receipt_key,
            )
            if exists:
                continue

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
                    "features": dict(row["features"] or {}),
                },
            )
            await _record_receipt(
                db,
                receipt_key=receipt_key,
                source_kind="neighbor_relation",
                source_id=source_id,
                scope_key=scope["scope_key"],
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
        projected = await reproject_existing_scope(db, fuseki, scope_key=scope_key)
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
        WITH ranked AS (
            SELECT
                n.*,
                row_number() OVER (
                    PARTITION BY n.scope_key, n.proposition_id
                    ORDER BY
                        CASE n.node_type
                            WHEN 'TOPIC' THEN 0
                            WHEN 'WORLD_ASSERTION' THEN 1
                            WHEN 'ACQUISITION' THEN 1
                            ELSE 2
                        END,
                        n.significance DESC,
                        n.created_at
                ) AS rn
            FROM aios.semantic_topology_node n
            JOIN aios.semantic_cluster_membership m
              ON m.proposition_id=n.proposition_id
            WHERE m.cluster_id=$1
        )
        SELECT
            scope_key,
            MIN(scope_kind) AS scope_kind,
            MIN(character_id) AS character_id,
            MIN(character_instance_id) AS character_instance_id,
            MIN(world_id) AS world_id,
            MIN(source_id) AS source_id,
            array_agg(topology_node_id ORDER BY topology_node_id) AS member_nodes,
            COUNT(*) AS member_count
        FROM ranked
        WHERE rn=1
        GROUP BY scope_key
        HAVING COUNT(*) >= 2
        """,
        cluster_id,
    )
    return [dict(row) for row in rows]


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
    affected_scopes: set[str] = set()

    for row in rows:
        scopes = await _cluster_scope_rows(db, cluster_id=row["cluster_id"])
        for scope in scopes:
            source_id = str(row["classification_id"])
            receipt_key = f"cluster:{source_id}:{scope['scope_key']}"
            exists = await db.fetchrow(
                "SELECT 1 FROM aios.semantic_reconciliation_receipt WHERE receipt_key=$1",
                receipt_key,
            )
            if exists:
                continue

            decision = _decision_from_row(scope)
            cluster_node = await _upsert_node(
                db,
                decision=decision,
                node_type="SEMANTIC_CLUSTER",
                node_key=f"cluster:{row['cluster_key']}",
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
                },
            )

            for member_node in scope["member_nodes"]:
                await _upsert_edge(
                    db,
                    decision=decision,
                    parent=cluster_node,
                    child=member_node,
                    edge_type="semantic_cluster_member",
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
                action="materialize_semantic_cluster",
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
        projected = await reproject_existing_scope(db, fuseki, scope_key=scope_key)
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
    scope_key: str,
    cluster_id: UUID,
) -> UUID | None:
    row = await db.fetchrow(
        """
        SELECT r.topology_node_id
        FROM aios.semantic_reconciliation_receipt r
        JOIN aios.semantic_cluster_classification cc
          ON cc.classification_id::text=r.source_id
        WHERE r.source_kind='cluster'
          AND r.scope_key=$1
          AND cc.cluster_id=$2
          AND r.topology_node_id IS NOT NULL
        ORDER BY r.reconciled_at DESC
        LIMIT 1
        """,
        scope_key,
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
    if candidate_kind == "world" and scope.get("world_id") is None:
        return

    await db.execute(
        """
        INSERT INTO aios.semantic_branch_candidate (
            boundary_classification_id, run_id, scope_key, scope_kind,
            candidate_kind, cluster_a_id, cluster_b_id,
            character_id, character_instance_id, world_id, timeline_id,
            confidence, status, reason, created_at, updated_at
        )
        VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
            'candidate',$13::jsonb,now(),now()
        )
        ON CONFLICT (boundary_classification_id, scope_key, candidate_kind)
        DO UPDATE SET
            confidence=GREATEST(aios.semantic_branch_candidate.confidence, EXCLUDED.confidence),
            reason=aios.semantic_branch_candidate.reason || EXCLUDED.reason,
            updated_at=now()
        """,
        classification_id,
        run_id,
        scope["scope_key"],
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
                n.scope_kind,
                n.character_id,
                n.character_instance_id,
                n.world_id,
                n.source_id
            FROM aios.semantic_reconciliation_receipt ra
            JOIN aios.semantic_cluster_classification cca
              ON cca.classification_id::text=ra.source_id
            JOIN aios.semantic_reconciliation_receipt rb
              ON rb.scope_key=ra.scope_key
             AND rb.source_kind='cluster'
            JOIN aios.semantic_cluster_classification ccb
              ON ccb.classification_id::text=rb.source_id
            JOIN aios.semantic_topology_node n
              ON n.scope_key=ra.scope_key
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
                scope_key=scope["scope_key"],
                cluster_id=row["cluster_a_id"],
            )
            node_b = await _cluster_node_for_scope(
                db,
                scope_key=scope["scope_key"],
                cluster_id=row["cluster_b_id"],
            )
            if node_a is None or node_b is None or node_a == node_b:
                continue

            edge_type = BOUNDARY_EDGE_TYPES[row["classification"]]
            receipt_key = f"boundary:{row['classification_id']}:{scope['scope_key']}"
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
                    "feature_scores": dict(row["feature_scores"] or {}),
                },
            )

            await _record_receipt(
                db,
                receipt_key=receipt_key,
                source_kind="boundary",
                source_id=str(row["classification_id"]),
                scope_key=scope["scope_key"],
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
                    evidence=dict(row["evidence"] or {}),
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
        projected = await reproject_existing_scope(db, fuseki, scope_key=scope_key)
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


async def reconcile_semantic_structure_once(
    db: Database,
    fuseki: FusekiClient,
    cfg: SemanticIndexConfig,
) -> int:
    pair_count = await reconcile_neighbor_relations_once(db, fuseki, cfg)
    cluster_count = await reconcile_clusters_once(db, fuseki, cfg)
    boundary_count = await reconcile_boundaries_once(db, fuseki, cfg)
    total = pair_count + cluster_count + boundary_count
    if total:
        logger.info(
            "Semantic reconciliation promoted %d pair relations, %d clusters, "
            "and %d boundaries into scope-safe topology/RDF",
            pair_count,
            cluster_count,
            boundary_count,
        )
    return total
