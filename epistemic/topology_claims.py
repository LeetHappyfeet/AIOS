from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient
from aios_app.epistemic.ownership import (
    SemanticOwnershipResolution,
    deterministic_semantic_ownership,
    resolve_character_mention,
    resolve_semantic_ownership,
)
from aios_app.epistemic.topology import (
    RESOLVER_VERSION,
    TopologyDecision,
    _project_scope_rdf,
    _rdf_graph,
    _upsert_edge,
    _upsert_node,
)


def _branch_profile(row: dict[str, Any]) -> tuple[str, float]:
    claim_kind = str(row.get("claim_kind") or "UNKNOWN").upper()
    family = str(row.get("predicate_family") or "UNKNOWN").upper()

    if family in {"EPISTEMIC", "MEMORY"} or claim_kind in {"BELIEF", "MEMORY"}:
        return "epistemic_transition", 0.95
    if claim_kind == "EVENT" or family in {"ACTION", "CAUSAL", "COMMUNICATION"}:
        return "event", 0.90
    if family == "TEMPORAL":
        return "temporal_transition", 0.85
    if row.get("subject_is_pivot") or row.get("object_is_pivot"):
        return "semantic_pivot", 0.75
    return "topic", 0.55


def _decision_from_resolution(
    row: dict[str, Any],
    resolution: SemanticOwnershipResolution,
) -> TopologyDecision:
    branch_kind, significance = _branch_profile(row)
    return TopologyDecision(
        scope_kind=resolution.owner_kind,
        scope_key=resolution.owner_key,
        branch_kind=branch_kind,
        significance=significance,
        character_id=resolution.character_id,
        character_instance_id=resolution.character_instance_id,
        world_id=resolution.world_id,
        source_id=resolution.source_id,
    )


def choose_observation_scope(row: dict[str, Any]) -> TopologyDecision:
    """Compatibility entry point for deterministic callers and unit tests."""
    return _decision_from_resolution(row, deterministic_semantic_ownership(row))


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
    ownership = await resolve_semantic_ownership(db, data)
    decision = _decision_from_resolution(data, ownership)
    ownership_meta = ownership.as_meta()
    projection_key = f"claim:{claim_id}:{decision.scope_key}"

    root = await _upsert_node(
        db,
        decision=decision,
        node_type="ROOT",
        node_key="root",
        label=decision.scope_key,
        timeline_id=data.get("timeline_id"),
        dag_node_id=None,
        proposition_id=None,
        claim_id=None,
        assertion_id=None,
        significance=1.0,
        meta={
            "resolver_version": RESOLVER_VERSION,
            "semantic_ownership": ownership_meta,
        },
    )

    branch_parent = root
    if decision.scope_kind == "character" and decision.character_instance_id:
        instance = await _upsert_node(
            db,
            decision=decision,
            node_type="INSTANCE",
            node_key=str(decision.character_instance_id),
            label=f"character-instance:{decision.character_instance_id}",
            timeline_id=data.get("timeline_id"),
            dag_node_id=data.get("forked_from_node_id"),
            proposition_id=None,
            claim_id=None,
            assertion_id=None,
            significance=1.0,
            meta={
                "parent_instance_id": str(data["parent_instance_id"])
                if data.get("parent_instance_id")
                else None,
                "forked_from_node_id": str(data["forked_from_node_id"])
                if data.get("forked_from_node_id")
                else None,
            },
        )
        branch_parent = instance
        if data.get("parent_instance_id"):
            parent_instance = await _upsert_node(
                db,
                decision=decision,
                node_type="INSTANCE",
                node_key=str(data["parent_instance_id"]),
                label=f"character-instance:{data['parent_instance_id']}",
                timeline_id=None,
                dag_node_id=data.get("forked_from_node_id"),
                proposition_id=None,
                claim_id=None,
                assertion_id=None,
                significance=1.0,
            )
            await _upsert_edge(
                db,
                decision=decision,
                parent=root,
                child=parent_instance,
                edge_type="experiential_branch",
                significance=1.0,
            )
            await _upsert_edge(
                db,
                decision=decision,
                parent=parent_instance,
                child=instance,
                edge_type="forks_at",
                significance=1.0,
                meta={
                    "forked_from_node_id": str(data["forked_from_node_id"])
                    if data.get("forked_from_node_id")
                    else None
                },
            )
        else:
            await _upsert_edge(
                db,
                decision=decision,
                parent=root,
                child=instance,
                edge_type="experiential_branch",
                significance=1.0,
            )

    anchor_key = str(data.get("dag_node_id") or data.get("observation_id"))
    anchor = await _upsert_node(
        db,
        decision=decision,
        node_type=decision.branch_kind.upper(),
        node_key=anchor_key,
        label=data.get("raw_text"),
        timeline_id=data.get("timeline_id"),
        dag_node_id=data.get("dag_node_id"),
        proposition_id=data.get("proposition_id"),
        claim_id=claim_id,
        assertion_id=None,
        significance=decision.significance,
        meta={
            "claim_kind": data.get("claim_kind"),
            "predicate_family": data.get("predicate_family"),
            "epistemic_scope": data.get("epistemic_scope"),
            "acquisition_mode": data.get("acquisition_mode"),
            "target_character_id": data.get("target_character_id"),
            "target_world_id": str(data["target_world_id"])
            if data.get("target_world_id")
            else None,
            "source_provenance": {
                "source_id": data.get("source_id"),
                "source_kind": data.get("source_kind"),
            },
            "semantic_ownership": ownership_meta,
        },
    )
    await _upsert_edge(
        db,
        decision=decision,
        parent=branch_parent,
        child=anchor,
        edge_type="contains_branch",
        significance=decision.significance,
        claim_id=claim_id,
    )

    topic = await _upsert_node(
        db,
        decision=decision,
        node_type="TOPIC",
        node_key=str(data["topic_key"]),
        label=str(data["topic_key"]),
        timeline_id=data.get("timeline_id"),
        dag_node_id=None,
        proposition_id=None,
        claim_id=None,
        assertion_id=None,
        significance=0.8,
        meta={"semantic_role": "topic_group"},
    )
    await _upsert_edge(
        db,
        decision=decision,
        parent=anchor,
        child=topic,
        edge_type=(
            "epistemic_transition"
            if decision.branch_kind == "epistemic_transition"
            else "about_topic"
        ),
        significance=decision.significance,
        claim_id=claim_id,
    )

    proposition = await _upsert_node(
        db,
        decision=decision,
        node_type="PROPOSITION",
        node_key=str(data["proposition_id"]),
        label=data.get("canonical_text"),
        timeline_id=data.get("timeline_id"),
        dag_node_id=data.get("dag_node_id"),
        proposition_id=data.get("proposition_id"),
        claim_id=claim_id,
        assertion_id=None,
        significance=0.82,
        meta={
            "semantic_role": "proposition_leaf",
            "topic_key": str(data["topic_key"]),
            "semantic_ownership": ownership_meta,
        },
    )
    await _upsert_edge(
        db,
        decision=decision,
        parent=topic,
        child=proposition,
        edge_type="topic_contains_proposition",
        significance=0.82,
        claim_id=claim_id,
    )

    for role, value, kind, is_pivot in (
        (
            "subject",
            data.get("subject_norm") or data.get("subject"),
            data.get("subject_kind"),
            data.get("subject_is_pivot"),
        ),
        (
            "object",
            data.get("object_norm") or data.get("object"),
            data.get("object_kind"),
            data.get("object_is_pivot"),
        ),
    ):
        if not value or not is_pivot:
            continue

        identity = await resolve_character_mention(db, str(value))
        if identity:
            node_type = "CHARACTER"
            node_key = f"character:{identity['character_id'].strip().lower()}"
            label = identity["label"]
            entity_meta = {
                "canonical_entity_id": f"character:{identity['character_id']}",
                "surface_mention": str(value),
                "identity_resolution": identity["method"],
                "ner_kind_overridden": str(kind) if kind else None,
            }
        else:
            node_type = str(kind or "ENTITY")
            node_key = f"{role}:{str(value).strip().lower()}"
            label = str(value)
            entity_meta = {"surface_mention": str(value)}

        entity = await _upsert_node(
            db,
            decision=decision,
            node_type=node_type,
            node_key=node_key,
            label=label,
            timeline_id=data.get("timeline_id"),
            dag_node_id=data.get("dag_node_id"),
            proposition_id=None,
            claim_id=claim_id,
            assertion_id=None,
            significance=0.75,
            meta=entity_meta,
        )
        await _upsert_edge(
            db,
            decision=decision,
            parent=anchor,
            child=entity,
            edge_type=f"{role}_pivot",
            significance=0.75,
            claim_id=claim_id,
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
            projection_key,
            claim_id,
            decision.scope_key,
            dataset,
            graph,
            RESOLVER_VERSION,
            json.dumps(
                {
                    "branch_kind": decision.branch_kind,
                    "semantic_ownership": ownership_meta,
                }
            ),
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
            projection_key,
            claim_id,
            decision.scope_key,
            *_rdf_graph(decision),
            RESOLVER_VERSION,
            repr(exc)[:2000],
        )
        raise

    return True
