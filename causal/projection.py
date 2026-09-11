from __future__ import annotations

from typing import Any, Optional
from uuid import UUID


async def project_committed_world_state(
    con: Any,
    *,
    world_id: UUID,
    timeline_id: UUID,
    domain_id: str,
    entity_id: UUID,
    state_key: str,
    value: Any,
    dag_node_id: Optional[UUID],
) -> None:
    """Project canonical causal state into objective /world SQL semantics.

    This module must never write character knowledge, beliefs, memories, or any
    other /char epistemic table.  /char acquisition remains a separate concern.
    """

    if domain_id != "world.location" or state_key != "location_entity_id":
        return

    location_id = UUID(str(value))
    endpoints = await con.fetch(
        """
        SELECT entity_id
        FROM aios.world_entity
        WHERE world_id=$1 AND entity_id = ANY($2::uuid[])
        """,
        world_id,
        [entity_id, location_id],
    )
    if len(endpoints) != 2:
        raise ValueError("location subject and target must both belong to the causal world")

    # The active located_in relation is a semantic /world projection.  Causal
    # state remains authoritative; RDF/HUD may consume this projection without
    # receiving direct access to causal_state.  When a structured sensor event
    # has no DAG node, deleting the stale projection is safe because immutable
    # history lives in world_event, not in this materialized relation.
    if dag_node_id is None:
        await con.execute(
            """
            DELETE FROM aios.world_entity_relation
            WHERE world_id=$1
              AND subject_entity_id=$2
              AND relation_type='located_in'
              AND valid_to_node_id IS NULL
              AND object_entity_id<>$3
              AND COALESCE(meta->>'source','') IN ('causal_projection','causal_bootstrap')
            """,
            world_id,
            entity_id,
            location_id,
        )
    else:
        await con.execute(
            """
            UPDATE aios.world_entity_relation
            SET valid_to_node_id=$4,
                meta=COALESCE(meta, '{}'::jsonb) || jsonb_build_object(
                    'closed_by','causal_projection',
                    'timeline_id',$3::text
                )
            WHERE world_id=$1
              AND subject_entity_id=$2
              AND relation_type='located_in'
              AND valid_to_node_id IS NULL
              AND object_entity_id<>$5
            """,
            world_id,
            entity_id,
            timeline_id,
            dag_node_id,
            location_id,
        )

    await con.execute(
        """
        INSERT INTO aios.world_entity_relation (
            world_id, subject_entity_id, relation_type, object_entity_id,
            valid_from_node_id, meta
        )
        SELECT $1,$2,'located_in',$3,$4,
               jsonb_build_object(
                   'source','causal_projection',
                   'timeline_id',$5::text
               )
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.world_entity_relation
            WHERE world_id=$1
              AND subject_entity_id=$2
              AND relation_type='located_in'
              AND object_entity_id=$3
              AND valid_to_node_id IS NULL
        )
        """,
        world_id,
        entity_id,
        location_id,
        dag_node_id,
        timeline_id,
    )
