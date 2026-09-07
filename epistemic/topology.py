from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote
from uuid import UUID

from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient

RESOLVER_VERSION = "semantic-topology-v1"
WORLD_GRAPH = "urn:aios:world:derived-topology"


def _json_object(value: object) -> dict:
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


@dataclass(frozen=True)
class TopologyDecision:
    scope_kind: str
    scope_key: str
    branch_kind: str
    significance: float
    character_id: Optional[str] = None
    character_instance_id: Optional[UUID] = None
    world_id: Optional[UUID] = None
    source_id: Optional[str] = None


def choose_observation_scope(row: dict[str, Any]) -> TopologyDecision:
    claim_kind = str(row.get("claim_kind") or "UNKNOWN").upper()
    family = str(row.get("predicate_family") or "UNKNOWN").upper()

    if (
        row.get("epistemic_scope") == "character"
        and row.get("origin_character_id")
        and row.get("character_instance_id")
    ):
        cid = str(row["origin_character_id"])
        iid = row["character_instance_id"]
        scope_kind = "character"
        scope_key = f"char:{cid}"
        character_id = cid
        character_instance_id = iid
        world_id = row.get("world_id")
        source_id = row.get("source_id")
    elif row.get("source_id"):
        source_id = str(row["source_id"])
        scope_kind = "source"
        scope_key = f"source:{source_id}"
        character_id = None
        character_instance_id = None
        world_id = None
    elif row.get("world_id"):
        wid = row["world_id"]
        scope_kind = "world"
        scope_key = f"world:{wid}:observed"
        character_id = None
        character_instance_id = None
        world_id = wid
        source_id = row.get("source_id")
    else:
        scope_kind = "source"
        fallback = row.get("timeline_id") or row.get("claim_id")
        scope_key = f"source:unresolved:{fallback}"
        character_id = None
        character_instance_id = None
        world_id = None
        source_id = None

    if family in {"EPISTEMIC", "MEMORY"} or claim_kind in {"BELIEF", "MEMORY"}:
        branch_kind, significance = "epistemic_transition", 0.95
    elif claim_kind == "EVENT" or family in {"ACTION", "CAUSAL", "COMMUNICATION"}:
        branch_kind, significance = "event", 0.90
    elif family == "TEMPORAL":
        branch_kind, significance = "temporal_transition", 0.85
    elif row.get("subject_is_pivot") or row.get("object_is_pivot"):
        branch_kind, significance = "semantic_pivot", 0.75
    else:
        branch_kind, significance = "topic", 0.55

    return TopologyDecision(
        scope_kind=scope_kind,
        scope_key=scope_key,
        branch_kind=branch_kind,
        significance=significance,
        character_id=character_id,
        character_instance_id=character_instance_id,
        world_id=world_id,
        source_id=source_id,
    )


async def _upsert_node(
    db: Database,
    *,
    decision: TopologyDecision,
    node_type: str,
    node_key: str,
    label: Optional[str],
    timeline_id: Optional[UUID],
    dag_node_id: Optional[UUID],
    proposition_id: Optional[UUID],
    claim_id: Optional[UUID],
    assertion_id: Optional[UUID],
    significance: float,
    acquisition_id: Optional[UUID] = None,
    meta: Optional[dict] = None,
) -> UUID:
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.semantic_topology_node (
            scope_key, scope_kind, node_type, node_key, label,
            character_id, character_instance_id, world_id, source_id,
            timeline_id, dag_node_id, proposition_id, claim_id, assertion_id,
            acquisition_id, significance, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb)
        ON CONFLICT (scope_key, node_type, node_key) DO UPDATE
        SET label=COALESCE(EXCLUDED.label, aios.semantic_topology_node.label),
            acquisition_id=COALESCE(aios.semantic_topology_node.acquisition_id, EXCLUDED.acquisition_id),
            significance=GREATEST(aios.semantic_topology_node.significance, EXCLUDED.significance),
            updated_at=now(),
            meta=aios.semantic_topology_node.meta || EXCLUDED.meta
        RETURNING topology_node_id
        """,
        decision.scope_key,
        decision.scope_kind,
        node_type,
        node_key,
        label,
        decision.character_id,
        decision.character_instance_id,
        decision.world_id,
        decision.source_id,
        timeline_id,
        dag_node_id,
        proposition_id,
        claim_id,
        assertion_id,
        acquisition_id,
        significance,
        json.dumps(meta or {}),
    )
    return row["topology_node_id"]


async def _upsert_edge(
    db: Database,
    *,
    decision: TopologyDecision,
    parent: UUID,
    child: UUID,
    edge_type: str,
    significance: float,
    claim_id: Optional[UUID] = None,
    assertion_id: Optional[UUID] = None,
    inference_source: str = "deterministic",
    inference_status: str = "accepted",
    inference_confidence: Optional[float] = None,
    meta: Optional[dict] = None,
) -> UUID:
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.semantic_topology_edge (
            scope_key, parent_node_id, child_node_id, edge_type,
            significance, claim_id, assertion_id,
            inference_source, inference_status, inference_confidence, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
        ON CONFLICT (scope_key, parent_node_id, child_node_id, edge_type) DO UPDATE
        SET significance=GREATEST(aios.semantic_topology_edge.significance, EXCLUDED.significance),
            inference_source=CASE
                WHEN aios.semantic_topology_edge.inference_source='deterministic'
                THEN aios.semantic_topology_edge.inference_source
                ELSE EXCLUDED.inference_source
            END,
            inference_status=CASE
                WHEN aios.semantic_topology_edge.inference_source='deterministic'
                THEN aios.semantic_topology_edge.inference_status
                ELSE EXCLUDED.inference_status
            END,
            inference_confidence=CASE
                WHEN aios.semantic_topology_edge.inference_source='deterministic'
                THEN aios.semantic_topology_edge.inference_confidence
                ELSE GREATEST(
                    COALESCE(aios.semantic_topology_edge.inference_confidence,0),
                    COALESCE(EXCLUDED.inference_confidence,0)
                )
            END,
            meta=aios.semantic_topology_edge.meta || EXCLUDED.meta
        RETURNING edge_id
        """,
        decision.scope_key,
        parent,
        child,
        edge_type,
        significance,
        claim_id,
        assertion_id,
        inference_source,
        inference_status,
        inference_confidence,
        json.dumps(meta or {}),
    )
    return row["edge_id"]


def _acquisition_anchor_relationship(acquisition_mode: Optional[str]) -> str:
    mode = (acquisition_mode or "").strip().lower()
    if "remember" in mode or "recall" in mode:
        return "remembers"
    if any(token in mode for token in ("read", "learn", "document", "source")):
        return "learned_from"
    if any(token in mode for token in ("observe", "perceive", "utterance", "chat", "heard", "saw")):
        return "perceived"
    return "references"


async def _upsert_anchor_edge(
    db: Database,
    *,
    source_scope_key: str,
    source_node_id: UUID,
    target_scope_key: str,
    target_node_id: UUID,
    relationship_type: str,
    character_id: Optional[str],
    character_instance_id: Optional[UUID],
    world_id: Optional[UUID],
    proposition_id: Optional[UUID],
    acquisition_id: Optional[UUID],
    dag_node_id: Optional[UUID],
    confidence: float = 1.0,
    inference_source: str = "deterministic",
    inference_status: str = "accepted",
    meta: Optional[dict] = None,
) -> UUID:
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.semantic_anchor_edge (
            source_scope_key, source_node_id, target_scope_key, target_node_id,
            relationship_type, character_id, character_instance_id, world_id,
            proposition_id, acquisition_id, dag_node_id, confidence,
            inference_source, inference_status, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb)
        ON CONFLICT (source_node_id, target_node_id, relationship_type) DO UPDATE
        SET confidence=GREATEST(aios.semantic_anchor_edge.confidence, EXCLUDED.confidence),
            inference_source=CASE
                WHEN aios.semantic_anchor_edge.inference_source='deterministic'
                THEN aios.semantic_anchor_edge.inference_source
                ELSE EXCLUDED.inference_source
            END,
            inference_status=CASE
                WHEN aios.semantic_anchor_edge.inference_source='deterministic'
                THEN aios.semantic_anchor_edge.inference_status
                ELSE EXCLUDED.inference_status
            END,
            meta=aios.semantic_anchor_edge.meta || EXCLUDED.meta,
            updated_at=now()
        RETURNING anchor_edge_id
        """,
        source_scope_key,
        source_node_id,
        target_scope_key,
        target_node_id,
        relationship_type,
        character_id,
        character_instance_id,
        world_id,
        proposition_id,
        acquisition_id,
        dag_node_id,
        confidence,
        inference_source,
        inference_status,
        json.dumps(meta or {}),
    )
    return row["anchor_edge_id"]


async def _find_world_topic_anchor(
    db: Database,
    *,
    world_id: UUID,
    proposition_id: UUID,
) -> Optional[dict[str, Any]]:
    row = await db.fetchrow(
        """
        SELECT topology_node_id, scope_key
        FROM aios.semantic_topology_node
        WHERE scope_kind='world'
          AND world_id=$1
          AND proposition_id=$2
          AND node_type='TOPIC'
        ORDER BY
            CASE
                WHEN scope_key=$3 THEN 0
                WHEN scope_key=$4 THEN 1
                ELSE 2
            END,
            significance DESC,
            created_at
        LIMIT 1
        """,
        world_id,
        proposition_id,
        f"world:{world_id}:asserted",
        f"world:{world_id}:observed",
    )
    return dict(row) if row else None


async def _anchor_character_node_to_world(
    db: Database,
    *,
    source_scope_key: str,
    source_node_id: UUID,
    character_id: str,
    character_instance_id: UUID,
    world_id: UUID,
    proposition_id: UUID,
    acquisition_id: UUID,
    acquisition_mode: Optional[str],
    dag_node_id: Optional[UUID],
) -> bool:
    target = await _find_world_topic_anchor(
        db,
        world_id=world_id,
        proposition_id=proposition_id,
    )
    if not target:
        return False
    await _upsert_anchor_edge(
        db,
        source_scope_key=source_scope_key,
        source_node_id=source_node_id,
        target_scope_key=target["scope_key"],
        target_node_id=target["topology_node_id"],
        relationship_type=_acquisition_anchor_relationship(acquisition_mode),
        character_id=character_id,
        character_instance_id=character_instance_id,
        world_id=world_id,
        proposition_id=proposition_id,
        acquisition_id=acquisition_id,
        dag_node_id=dag_node_id,
        meta={"anchor_policy": "world_topic_same_proposition"},
    )
    return True


async def _backfill_character_anchors_for_world_topic(
    db: Database,
    *,
    world_id: UUID,
    proposition_id: UUID,
    target_scope_key: str,
    target_node_id: UUID,
) -> set[str]:
    rows = await db.fetch(
        """
        SELECT n.topology_node_id, n.scope_key, n.character_id,
               n.character_instance_id, n.dag_node_id, n.acquisition_id,
               kae.acquisition_mode
        FROM aios.semantic_topology_node n
        JOIN aios.knowledge_acquisition_event kae
          ON kae.acquisition_id=n.acquisition_id
        WHERE n.scope_kind='character'
          AND n.node_type='EPISTEMIC_TRANSITION'
          AND n.world_id=$1
          AND n.proposition_id=$2
          AND n.acquisition_id IS NOT NULL
        """,
        world_id,
        proposition_id,
    )
    touched: set[str] = set()
    for row in rows:
        await _upsert_anchor_edge(
            db,
            source_scope_key=row["scope_key"],
            source_node_id=row["topology_node_id"],
            target_scope_key=target_scope_key,
            target_node_id=target_node_id,
            relationship_type=_acquisition_anchor_relationship(row["acquisition_mode"]),
            character_id=row["character_id"],
            character_instance_id=row["character_instance_id"],
            world_id=world_id,
            proposition_id=proposition_id,
            acquisition_id=row["acquisition_id"],
            dag_node_id=row["dag_node_id"],
            meta={"anchor_policy": "world_topic_backfill"},
        )
        touched.add(row["scope_key"])
    return touched


def _rdf_graph(decision: TopologyDecision) -> tuple[str, str]:
    scope_segment = quote(decision.scope_key, safe="")
    if decision.scope_kind == "character" and decision.character_id:
        cid = quote(decision.character_id, safe="")
        return "char", f"urn:aios:char:{cid}:topology:{scope_segment}"
    return "world", f"{WORLD_GRAPH}:{scope_segment}"


async def _project_scope_rdf(
    db: Database,
    fuseki: FusekiClient,
    *,
    decision: TopologyDecision,
) -> tuple[str, str]:
    dataset, graph = _rdf_graph(decision)
    rows = await db.fetch(
        """
        SELECT n.topology_node_id, n.node_type, n.node_key, n.label, n.significance,
               e.edge_id, e.parent_node_id, e.edge_type,
               e.significance AS edge_significance,
               e.inference_source, e.inference_status, e.inference_confidence,
               e.meta AS edge_meta
        FROM aios.semantic_topology_node n
        LEFT JOIN aios.semantic_topology_edge e
          ON e.scope_key=n.scope_key AND e.child_node_id=n.topology_node_id
        WHERE n.scope_key=$1
        ORDER BY n.created_at, n.topology_node_id
        """,
        decision.scope_key,
    )
    scope_iri = f"urn:aios:topology-scope:{quote(decision.scope_key, safe='')}"
    triples = [
        f"<{scope_iri}> <urn:aios:topology#scopeKind> {json.dumps(decision.scope_kind)} .",
        f"<{scope_iri}> <urn:aios:topology#scopeKey> {json.dumps(decision.scope_key)} .",
    ]
    for row in rows:
        node_iri = f"urn:aios:topology-node:{row['topology_node_id']}"
        triples.append(f"<{scope_iri}> <urn:aios:topology#hasNode> <{node_iri}> .")
        triples.append(f"<{node_iri}> <urn:aios:topology#nodeType> {json.dumps(row['node_type'])} .")
        triples.append(f"<{node_iri}> <urn:aios:topology#nodeKey> {json.dumps(row['node_key'])} .")
        if row["label"]:
            triples.append(f"<{node_iri}> <urn:aios:topology#label> {json.dumps(row['label'])} .")
        if row["parent_node_id"]:
            parent_iri = f"urn:aios:topology-node:{row['parent_node_id']}"
            pred = quote(str(row["edge_type"]), safe="")
            triples.append(f"<{parent_iri}> <urn:aios:topology#{pred}> <{node_iri}> .")
            if row["edge_id"]:
                edge_iri = f"urn:aios:topology-edge:{row['edge_id']}"
                triples.append(f"<{scope_iri}> <urn:aios:topology#hasEdge> <{edge_iri}> .")
                triples.append(f"<{edge_iri}> <urn:aios:topology#fromNode> <{parent_iri}> .")
                triples.append(f"<{edge_iri}> <urn:aios:topology#toNode> <{node_iri}> .")
                triples.append(
                    f"<{edge_iri}> <urn:aios:topology#edgeType> "
                    f"{json.dumps(str(row['edge_type']))} ."
                )
                triples.append(
                    f"<{edge_iri}> <urn:aios:topology#inferenceSource> "
                    f"{json.dumps(str(row['inference_source'] or 'deterministic'))} ."
                )
                triples.append(
                    f"<{edge_iri}> <urn:aios:topology#inferenceStatus> "
                    f"{json.dumps(str(row['inference_status'] or 'accepted'))} ."
                )
                if row["inference_confidence"] is not None:
                    triples.append(
                        f"<{edge_iri}> <urn:aios:topology#inferenceConfidence> "
                        f"\"{float(row['inference_confidence'])}\"^^"
                        f"<http://www.w3.org/2001/XMLSchema#double> ."
                    )
                if row["edge_meta"]:
                    triples.append(
                        f"<{edge_iri}> <urn:aios:topology#inferenceMeta> "
                        f"{json.dumps(json.dumps(_json_object(row['edge_meta']), sort_keys=True))} ."
                    )

    anchor_rows = await db.fetch(
        """
        SELECT anchor_edge_id, source_node_id, target_node_id,
               relationship_type, target_scope_key, confidence,
               inference_source, inference_status, meta
        FROM aios.semantic_anchor_edge
        WHERE source_scope_key=$1
        ORDER BY created_at, anchor_edge_id
        """,
        decision.scope_key,
    )
    for anchor in anchor_rows:
        source_iri = f"urn:aios:topology-node:{anchor['source_node_id']}"
        target_iri = f"urn:aios:topology-node:{anchor['target_node_id']}"
        edge_iri = f"urn:aios:semantic-anchor:{anchor['anchor_edge_id']}"
        pred = quote(str(anchor["relationship_type"]), safe="")
        triples.append(f"<{source_iri}> <urn:aios:anchor#{pred}> <{target_iri}> .")
        triples.append(f"<{scope_iri}> <urn:aios:anchor#hasAnchor> <{edge_iri}> .")
        triples.append(f"<{edge_iri}> <urn:aios:anchor#fromNode> <{source_iri}> .")
        triples.append(f"<{edge_iri}> <urn:aios:anchor#toNode> <{target_iri}> .")
        triples.append(
            f"<{edge_iri}> <urn:aios:anchor#targetScope> "
            f"{json.dumps(str(anchor['target_scope_key']))} ."
        )
        triples.append(
            f"<{edge_iri}> <urn:aios:anchor#relationshipType> "
            f"{json.dumps(str(anchor['relationship_type']))} ."
        )
        triples.append(
            f"<{edge_iri}> <urn:aios:anchor#confidence> "
            f"\"{float(anchor['confidence'])}\"^^"
            f"<http://www.w3.org/2001/XMLSchema#double> ."
        )
        triples.append(
            f"<{edge_iri}> <urn:aios:anchor#inferenceSource> "
            f"{json.dumps(str(anchor['inference_source']))} ."
        )
        triples.append(
            f"<{edge_iri}> <urn:aios:anchor#inferenceStatus> "
            f"{json.dumps(str(anchor['inference_status']))} ."
        )

    sparql = f"""
CLEAR SILENT GRAPH <{graph}>;
INSERT DATA {{ GRAPH <{graph}> {{ {' '.join(triples)} }} }}
"""
    fuseki.update(dataset, sparql)
    return dataset, graph


async def derive_claim_topology(
    db: Database,
    fuseki: FusekiClient,
    *,
    claim_id: UUID,
) -> bool:
    row = await db.fetchrow(
        """
        SELECT
            ccr.*, cc.subject, cc.object, cc.raw_text,
            o.observation_id, o.proposition_id,
            p.topic_key, p.canonical_text, p.subject_norm, p.object_norm,
            ci.parent_instance_id, ci.forked_from_node_id
        FROM aios.claim_context_resolution ccr
        JOIN aios.claim_candidate cc ON cc.claim_id=ccr.claim_id
        JOIN aios.observation o ON o.claim_id=ccr.claim_id
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        LEFT JOIN aios.character_instance ci ON ci.instance_id=ccr.character_instance_id
        WHERE ccr.claim_id=$1
        """,
        claim_id,
    )
    if not row:
        return False

    data = dict(row)
    decision = choose_observation_scope(data)
    projection_key = f"claim:{claim_id}:{decision.scope_key}"

    root = await _upsert_node(
        db, decision=decision, node_type="ROOT", node_key="root",
        label=decision.scope_key, timeline_id=data.get("timeline_id"),
        dag_node_id=None, proposition_id=None, claim_id=None, assertion_id=None,
        significance=1.0, meta={"resolver_version": RESOLVER_VERSION},
    )
    branch_parent = root
    if decision.scope_kind == "character" and decision.character_instance_id:
        instance = await _upsert_node(
            db, decision=decision, node_type="INSTANCE",
            node_key=str(decision.character_instance_id),
            label=f"character-instance:{decision.character_instance_id}",
            timeline_id=data.get("timeline_id"),
            dag_node_id=data.get("forked_from_node_id"),
            proposition_id=None, claim_id=None, assertion_id=None,
            significance=1.0,
            meta={
                "parent_instance_id": str(data["parent_instance_id"]) if data.get("parent_instance_id") else None,
                "forked_from_node_id": str(data["forked_from_node_id"]) if data.get("forked_from_node_id") else None,
            },
        )
        branch_parent = instance
        if data.get("parent_instance_id"):
            parent_instance = await _upsert_node(
                db, decision=decision, node_type="INSTANCE",
                node_key=str(data["parent_instance_id"]),
                label=f"character-instance:{data['parent_instance_id']}",
                timeline_id=None, dag_node_id=data.get("forked_from_node_id"),
                proposition_id=None, claim_id=None, assertion_id=None,
                significance=1.0,
            )
            await _upsert_edge(
                db, decision=decision, parent=root, child=parent_instance,
                edge_type="experiential_branch", significance=1.0,
            )
            await _upsert_edge(
                db, decision=decision, parent=parent_instance, child=instance,
                edge_type="forks_at", significance=1.0,
                meta={"forked_from_node_id": str(data["forked_from_node_id"]) if data.get("forked_from_node_id") else None},
            )
        else:
            await _upsert_edge(
                db, decision=decision, parent=root, child=instance,
                edge_type="experiential_branch", significance=1.0,
            )

    anchor_key = str(data.get("dag_node_id") or data.get("observation_id"))
    anchor = await _upsert_node(
        db, decision=decision, node_type=decision.branch_kind.upper(),
        node_key=anchor_key, label=data.get("raw_text"),
        timeline_id=data.get("timeline_id"), dag_node_id=data.get("dag_node_id"),
        proposition_id=data.get("proposition_id"), claim_id=claim_id, assertion_id=None,
        significance=decision.significance,
        meta={
            "claim_kind": data.get("claim_kind"),
            "predicate_family": data.get("predicate_family"),
            "epistemic_scope": data.get("epistemic_scope"),
            "acquisition_mode": data.get("acquisition_mode"),
            "target_character_id": data.get("target_character_id"),
            "target_world_id": str(data["target_world_id"]) if data.get("target_world_id") else None,
        },
    )
    await _upsert_edge(
        db, decision=decision, parent=branch_parent, child=anchor,
        edge_type="contains_branch", significance=decision.significance, claim_id=claim_id,
    )

    topic = await _upsert_node(
        db, decision=decision, node_type="TOPIC", node_key=str(data["topic_key"]),
        label=data.get("canonical_text"), timeline_id=data.get("timeline_id"),
        dag_node_id=data.get("dag_node_id"), proposition_id=data.get("proposition_id"),
        claim_id=claim_id, assertion_id=None, significance=0.8,
    )
    await _upsert_edge(
        db, decision=decision, parent=anchor, child=topic,
        edge_type=(
            "epistemic_transition" if decision.branch_kind == "epistemic_transition"
            else "about_topic"
        ),
        significance=decision.significance, claim_id=claim_id,
    )

    for role, value, kind, is_pivot in (
        ("subject", data.get("subject_norm") or data.get("subject"), data.get("subject_kind"), data.get("subject_is_pivot")),
        ("object", data.get("object_norm") or data.get("object"), data.get("object_kind"), data.get("object_is_pivot")),
    ):
        if not value or not is_pivot:
            continue
        entity = await _upsert_node(
            db, decision=decision, node_type=str(kind or "ENTITY"),
            node_key=f"{role}:{str(value).strip().lower()}", label=str(value),
            timeline_id=data.get("timeline_id"), dag_node_id=data.get("dag_node_id"),
            proposition_id=None, claim_id=claim_id, assertion_id=None,
            significance=0.75,
        )
        await _upsert_edge(
            db, decision=decision, parent=anchor, child=entity,
            edge_type=f"{role}_pivot", significance=0.75, claim_id=claim_id,
        )

    try:
        dataset, graph = await _project_scope_rdf(db, fuseki, decision=decision)
        await db.execute(
            """
            INSERT INTO aios.semantic_topology_projection (
                projection_key, claim_id, scope_key, rdf_dataset, rdf_graph,
                resolver_version, projected_at, last_error, meta
            )
            VALUES ($1,$2,$3,$4,$5,$6,now(),NULL,$7::jsonb)
            ON CONFLICT (projection_key) DO UPDATE
            SET projected_at=now(), last_error=NULL, updated_at=now(), meta=EXCLUDED.meta
            """,
            projection_key, claim_id, decision.scope_key, dataset, graph,
            RESOLVER_VERSION,
            json.dumps({"branch_kind": decision.branch_kind}),
        )
    except Exception as exc:
        await db.execute(
            """
            INSERT INTO aios.semantic_topology_projection (
                projection_key, claim_id, scope_key, rdf_dataset, rdf_graph,
                resolver_version, projected_at, last_error
            )
            VALUES ($1,$2,$3,$4,$5,$6,NULL,$7)
            ON CONFLICT (projection_key) DO UPDATE
            SET last_error=EXCLUDED.last_error, updated_at=now()
            """,
            projection_key, claim_id, decision.scope_key, *_rdf_graph(decision),
            RESOLVER_VERSION, repr(exc)[:2000],
        )
        raise
    return True


async def derive_world_assertion_topology(
    db: Database,
    fuseki: FusekiClient,
    *,
    assertion_id: UUID,
) -> bool:
    row = await db.fetchrow(
        """
        SELECT a.assertion_id, a.world_id, a.proposition_id, a.epistemic_status,
               a.source_kind, a.generated_at_node_id, a.reason,
               p.topic_key, p.canonical_text
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        WHERE a.assertion_id=$1
        """,
        assertion_id,
    )
    if not row:
        return False
    data = dict(row)
    decision = TopologyDecision(
        scope_kind="world",
        scope_key=f"world:{data['world_id']}:asserted",
        branch_kind="world_assertion",
        significance=0.95,
        world_id=data["world_id"],
    )
    projection_key = f"assertion:{assertion_id}:{decision.scope_key}"
    root = await _upsert_node(
        db, decision=decision, node_type="ROOT", node_key="root",
        label=decision.scope_key, timeline_id=None, dag_node_id=None,
        proposition_id=None, claim_id=None, assertion_id=None,
        significance=1.0,
    )
    assertion = await _upsert_node(
        db, decision=decision, node_type="WORLD_ASSERTION",
        node_key=str(assertion_id), label=data["canonical_text"],
        timeline_id=None, dag_node_id=data["generated_at_node_id"],
        proposition_id=data["proposition_id"], claim_id=None,
        assertion_id=assertion_id, significance=0.95,
        meta={
            "epistemic_status": data["epistemic_status"],
            "source_kind": data["source_kind"],
            "reason": data["reason"],
        },
    )
    topic = await _upsert_node(
        db, decision=decision, node_type="TOPIC", node_key=str(data["topic_key"]),
        label=data["canonical_text"], timeline_id=None,
        dag_node_id=data["generated_at_node_id"], proposition_id=data["proposition_id"],
        claim_id=None, assertion_id=assertion_id, significance=0.85,
    )
    await _upsert_edge(
        db, decision=decision, parent=root, child=assertion,
        edge_type="contains_assertion", significance=0.95, assertion_id=assertion_id,
    )
    await _upsert_edge(
        db, decision=decision, parent=assertion, child=topic,
        edge_type="asserts_topic", significance=0.95, assertion_id=assertion_id,
    )
    touched_character_scopes = await _backfill_character_anchors_for_world_topic(
        db,
        world_id=data["world_id"],
        proposition_id=data["proposition_id"],
        target_scope_key=decision.scope_key,
        target_node_id=topic,
    )

    dataset, graph = await _project_scope_rdf(db, fuseki, decision=decision)
    await db.execute(
        """
        INSERT INTO aios.semantic_topology_projection (
            projection_key, assertion_id, scope_key, rdf_dataset, rdf_graph,
            resolver_version, projected_at, last_error, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,now(),NULL,$7::jsonb)
        ON CONFLICT (projection_key) DO UPDATE
        SET projected_at=now(), last_error=NULL, updated_at=now(), meta=EXCLUDED.meta
        """,
        projection_key, assertion_id, decision.scope_key, dataset, graph,
        RESOLVER_VERSION, json.dumps({"branch_kind": "world_assertion"}),
    )
    for character_scope in touched_character_scopes:
        await reproject_existing_scope(
            db,
            fuseki,
            scope_key=character_scope,
        )
    return True


async def derive_character_acquisition_topology(
    db: Database,
    fuseki: FusekiClient,
    *,
    acquisition_id: UUID,
) -> bool:
    row = await db.fetchrow(
        """
        SELECT
            kae.acquisition_id, kae.instance_id, kae.proposition_id, kae.claim_id,
            kae.acquisition_mode, kae.epistemic_status, kae.confidence,
            kae.source_entity_id, kae.dag_node_id, kae.meta,
            ci.character_id, ci.current_world_id, ci.world_id,
            ci.parent_instance_id, ci.forked_from_node_id,
            p.topic_key, p.canonical_text,
            o.source_key, ccr.source_id, ccr.source_kind
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.character_instance ci ON ci.instance_id=kae.instance_id
        LEFT JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
        LEFT JOIN LATERAL (
            SELECT o.source_key
            FROM aios.observation o
            WHERE o.proposition_id=kae.proposition_id
            ORDER BY o.observed_at DESC
            LIMIT 1
        ) o ON true
        LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=kae.claim_id
        WHERE kae.acquisition_id=$1
        """,
        acquisition_id,
    )
    if not row or row["proposition_id"] is None:
        return False

    data = dict(row)
    decision = TopologyDecision(
        scope_kind="character",
        scope_key=f"char:{data['character_id']}",
        branch_kind="epistemic_transition",
        significance=0.95,
        character_id=data["character_id"],
        character_instance_id=data["instance_id"],
        world_id=data["current_world_id"] or data["world_id"],
        source_id=data.get("source_id"),
    )
    projection_key = f"acquisition:{acquisition_id}:{decision.scope_key}"

    root = await _upsert_node(
        db, decision=decision, node_type="ROOT", node_key="root",
        label=decision.scope_key, timeline_id=None, dag_node_id=None,
        proposition_id=None, claim_id=None, assertion_id=None,
        significance=1.0,
    )
    instance = await _upsert_node(
        db, decision=decision, node_type="INSTANCE", node_key=str(data["instance_id"]),
        label=f"character-instance:{data['instance_id']}", timeline_id=None,
        dag_node_id=data.get("forked_from_node_id"), proposition_id=None,
        claim_id=None, assertion_id=None, significance=1.0,
        meta={
            "parent_instance_id": str(data["parent_instance_id"]) if data.get("parent_instance_id") else None,
            "forked_from_node_id": str(data["forked_from_node_id"]) if data.get("forked_from_node_id") else None,
        },
    )
    await _upsert_edge(
        db, decision=decision, parent=root, child=instance,
        edge_type="experiential_branch", significance=1.0,
    )

    acquisition = await _upsert_node(
        db, decision=decision, node_type="EPISTEMIC_TRANSITION",
        node_key=f"acquisition:{acquisition_id}",
        label=data.get("canonical_text"), timeline_id=None,
        dag_node_id=data.get("dag_node_id"), proposition_id=data["proposition_id"],
        claim_id=data.get("claim_id"), assertion_id=None, significance=0.95,
        acquisition_id=acquisition_id,
        meta={
            "acquisition_id": str(acquisition_id),
            "acquisition_mode": data["acquisition_mode"],
            "epistemic_status": data["epistemic_status"],
            "source_id": data.get("source_id"),
            "source_kind": data.get("source_kind"),
            "source_key": data.get("source_key"),
        },
    )
    await _upsert_edge(
        db, decision=decision, parent=instance, child=acquisition,
        edge_type="acquires", significance=0.95, claim_id=data.get("claim_id"),
    )

    topic = await _upsert_node(
        db, decision=decision, node_type="TOPIC", node_key=str(data["topic_key"]),
        label=data["canonical_text"], timeline_id=None,
        dag_node_id=data.get("dag_node_id"), proposition_id=data["proposition_id"],
        claim_id=data.get("claim_id"), assertion_id=None, significance=0.85,
    )
    await _upsert_edge(
        db, decision=decision, parent=acquisition, child=topic,
        edge_type="epistemic_transition", significance=0.95,
        claim_id=data.get("claim_id"),
    )

    await _anchor_character_node_to_world(
        db,
        source_scope_key=decision.scope_key,
        source_node_id=acquisition,
        character_id=data["character_id"],
        character_instance_id=data["instance_id"],
        world_id=decision.world_id,
        proposition_id=data["proposition_id"],
        acquisition_id=acquisition_id,
        acquisition_mode=data.get("acquisition_mode"),
        dag_node_id=data.get("dag_node_id"),
    )

    if data.get("source_id") or data.get("source_key"):
        source_key = str(data.get("source_id") or data.get("source_key"))
        source_node = await _upsert_node(
            db, decision=decision, node_type="SOURCE_REFERENCE",
            node_key=f"source:{source_key}", label=source_key,
            timeline_id=None, dag_node_id=data.get("dag_node_id"),
            proposition_id=None, claim_id=data.get("claim_id"),
            assertion_id=None, significance=0.8,
            meta={"source_kind": data.get("source_kind")},
        )
        await _upsert_edge(
            db, decision=decision, parent=acquisition, child=source_node,
            edge_type="acquired_from", significance=0.85,
            claim_id=data.get("claim_id"),
        )

    dataset, graph = await _project_scope_rdf(db, fuseki, decision=decision)
    await db.execute(
        """
        INSERT INTO aios.semantic_topology_projection (
            projection_key, claim_id, acquisition_id, scope_key,
            rdf_dataset, rdf_graph, resolver_version, projected_at,
            last_error, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,now(),NULL,$8::jsonb)
        ON CONFLICT (projection_key) DO UPDATE
        SET projected_at=now(), last_error=NULL, updated_at=now(), meta=EXCLUDED.meta
        """,
        projection_key, data.get("claim_id"), acquisition_id, decision.scope_key,
        dataset, graph, RESOLVER_VERSION,
        json.dumps({"branch_kind": "epistemic_transition"}),
    )
    return True


async def reproject_existing_scope(
    db: Database,
    fuseki: FusekiClient,
    *,
    scope_key: str,
) -> tuple[str, str] | None:
    """Reproject one already-authorized topology scope after reconciliation."""
    row = await db.fetchrow(
        """
        SELECT scope_kind, scope_key, character_id, character_instance_id,
               world_id, source_id
        FROM aios.semantic_topology_node
        WHERE scope_key=$1
        ORDER BY created_at
        LIMIT 1
        """,
        scope_key,
    )
    if not row:
        return None

    decision = TopologyDecision(
        scope_kind=row["scope_kind"],
        scope_key=row["scope_key"],
        branch_kind="semantic_reconciliation",
        significance=0.7,
        character_id=row["character_id"],
        character_instance_id=row["character_instance_id"],
        world_id=row["world_id"],
        source_id=row["source_id"],
    )
    return await _project_scope_rdf(db, fuseki, decision=decision)
