"""Project source-admitted narrated occurrences into the existing RDF topology outbox."""
from __future__ import annotations

import re
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.topology import TopologyDecision, _upsert_node
from aios_app.epistemic.topology_projection import mark_scope_dirty


async def _world_entity(
    db: Database, world_id: UUID, entity_key: str | None, label: str | None,
) -> str | None:
    """Link existing world entities only when the supplied identity is unique."""
    if not entity_key and not label:
        return None
    rows = await db.fetch(
        """
        SELECT we.entity_id, CASE
            WHEN lower(we.entity_key)=lower($2) THEN 0
            WHEN lower(we.display_name)=lower($3) OR lower(ci.character_id)=lower($3) THEN 1
            ELSE 2 END AS match_rank
        FROM aios.world_entity we
        LEFT JOIN aios.character_instance ci ON ci.instance_id=we.character_instance_id
        WHERE we.world_id=$1 AND (
            lower(we.entity_key)=lower($2) OR lower(we.display_name)=lower($3)
            OR lower(ci.character_id)=lower($3) OR lower(we.display_name)=lower($4)
            OR lower(we.entity_key)=lower($4)
        )
        ORDER BY match_rank,we.entity_id LIMIT 2
        """,
        world_id, entity_key, label, re.sub(r"^(?:a|an|the)\s+", "", label, flags=re.I) if label else None,
    )
    if not rows or (len(rows) > 1 and rows[0]["match_rank"] == rows[1]["match_rank"]):
        return None
    return str(rows[0]["entity_id"])


async def project_narrated_scene_events(
    db: Database, *, claim_id: UUID | None = None, limit: int = 500,
) -> None:
    """Backfill one node per occurrence. Preserve source evidence and causal state."""
    rows = await db.fetch(
        """
        SELECT DISTINCT ON (s.scope_key,s.semantic_event_id) s.*
        FROM aios.narrated_scene_occurrence s
        WHERE ($1::uuid IS NULL OR s.claim_id=$1)
          AND NOT EXISTS (
              SELECT 1 FROM aios.semantic_topology_node n
              WHERE n.scope_key=s.scope_key AND n.node_type='NARRATED_SCENE_EVENT'
                AND n.node_key=s.semantic_event_id::text
          )
        ORDER BY s.scope_key,s.semantic_event_id,s.observation_id
        LIMIT $2
        """,
        claim_id, limit,
    )
    for row in rows:
        subject = row["resolved_subject"] or row["subject_norm"]
        obj = row["resolved_object"] or row["object_norm"]
        decision = TopologyDecision(
            scope_kind="world", scope_key=row["scope_key"], branch_kind="event",
            significance=row["confidence"], world_id=row["scene_world_id"],
        )
        async with db.atomic():
            await _upsert_node(
                db, decision=decision, node_type="NARRATED_SCENE_EVENT",
                node_key=str(row["semantic_event_id"]), label=row["canonical_text"],
                timeline_id=row["scene_timeline_id"], dag_node_id=row["dag_node_id"],
                proposition_id=row["proposition_id"], claim_id=row["claim_id"],
                assertion_id=None, significance=row["confidence"],
                meta={
                    "observation_id": str(row["observation_id"]),
                    "source_timeline_id": str(row["timeline_id"]),
                    "action": row["predicate_norm"], "movement": row["movement"],
                    "actor_label": subject, "object_label": obj,
                    "actor_entity_id": await _world_entity(
                        db, row["scene_world_id"], row["subject_entity_key"], subject),
                    "object_entity_id": await _world_entity(
                        db, row["scene_world_id"], row["object_entity_key"], obj),
                },
            )
            await mark_scope_dirty(db, decision=decision)
