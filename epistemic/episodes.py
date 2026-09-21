from __future__ import annotations

import json
from uuid import UUID

from aios_app.db import Database

EVENT_RESOLVER_VERSION = "semantic-event-resolver-v2"
EPISODE_RESOLVER_VERSION = "semantic-episode-resolver-v1"


async def materialize_event_occurrences_once(
    db: Database, *, claim_id: UUID | None = None, limit: int = 500
) -> int:
    """Materialize EVENT observations as occurrence identities.

    SAME_EVENT remains a separate reconciliation decision.  A unique occurrence
    does not need a duplicate/paraphrase before it can exist.
    """
    rows = await db.fetch(
        """
        SELECT
            o.observation_id, o.proposition_id, o.claim_id, o.timeline_id,
            o.dag_node_id, o.extraction_confidence,
            ccr.world_id
        FROM aios.observation o
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
        WHERE upper(COALESCE(ccr.claim_kind,''))='EVENT'
          AND ($1::uuid IS NULL OR o.claim_id=$1)
          AND NOT EXISTS (
              SELECT 1 FROM aios.semantic_event_membership m
              WHERE m.observation_id=o.observation_id AND m.status='active'
          )
        ORDER BY o.observed_at, o.observation_id
        LIMIT $2
        """,
        claim_id,
        limit,
    )
    written = 0
    for row in rows:
        confidence = max(0.0, min(1.0, float(row["extraction_confidence"] or 0.5)))
        event_key = f"observation:{row['observation_id']}"
        event = await db.fetchrow(
            """
            INSERT INTO aios.semantic_event (
                event_key, world_id, timeline_id, dag_node_id, status,
                confidence, resolver_version, meta, created_at, updated_at
            )
            VALUES ($1,$2,$3,$4,'active',$5,$6,$7::jsonb,now(),now())
            ON CONFLICT (event_key) DO UPDATE
            SET confidence=GREATEST(aios.semantic_event.confidence,EXCLUDED.confidence),
                updated_at=now()
            RETURNING semantic_event_id
            """,
            event_key, row["world_id"], row["timeline_id"], row["dag_node_id"],
            confidence, EVENT_RESOLVER_VERSION,
            json.dumps({"source": "event_observation", "observation_id": str(row["observation_id"])}),
        )
        await db.execute(
            """
            INSERT INTO aios.semantic_event_membership (
                semantic_event_id, proposition_id, observation_id, claim_id,
                membership_confidence, assigned_by, evidence_source_id,
                status, meta, created_at, updated_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,'active','{}'::jsonb,now(),now())
            ON CONFLICT (semantic_event_id, proposition_id) DO UPDATE
            SET observation_id=EXCLUDED.observation_id,
                claim_id=EXCLUDED.claim_id,
                membership_confidence=GREATEST(
                    aios.semantic_event_membership.membership_confidence,
                    EXCLUDED.membership_confidence
                ),
                assigned_by=EXCLUDED.assigned_by,
                status='active',
                updated_at=now()
            """,
            event["semantic_event_id"], row["proposition_id"], row["observation_id"],
            row["claim_id"], confidence, EVENT_RESOLVER_VERSION,
            f"observation:{row['observation_id']}",
        )
        written += 1
    return written


async def derive_semantic_episodes_once(db: Database, *, limit: int = 500) -> int:
    """Conservatively group occurrences by world/timeline/DAG source turn.

    DAG-node grouping is intentionally precise.  Cross-node continuity can be
    added later from explicit boundary/continuity evidence without corrupting
    occurrence identity.
    """
    rows = await db.fetch(
        """
        SELECT se.semantic_event_id, se.world_id, se.timeline_id, se.dag_node_id,
               se.confidence, MIN(o.observed_at) AS observed_at
        FROM aios.semantic_event se
        JOIN aios.semantic_event_membership sem
          ON sem.semantic_event_id=se.semantic_event_id AND sem.status='active'
        LEFT JOIN aios.observation o ON o.observation_id=sem.observation_id
        WHERE se.status='active'
          AND se.dag_node_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM aios.semantic_episode_membership em
              WHERE em.semantic_event_id=se.semantic_event_id AND em.status='active'
          )
        GROUP BY se.semantic_event_id
        ORDER BY MIN(o.observed_at) NULLS LAST, se.created_at
        LIMIT $1
        """,
        limit,
    )
    written = 0
    for row in rows:
        episode_key = (
            f"dag:{row['world_id'] or 'none'}:"
            f"{row['timeline_id'] or 'none'}:{row['dag_node_id']}"
        )
        episode = await db.fetchrow(
            """
            INSERT INTO aios.semantic_episode (
                episode_key, world_id, timeline_id, start_dag_node_id,
                end_dag_node_id, status, confidence, resolver_version, meta,
                created_at, updated_at
            )
            VALUES ($1,$2,$3,$4,$4,'active',$5,$6,$7::jsonb,now(),now())
            ON CONFLICT (episode_key) DO UPDATE
            SET confidence=GREATEST(aios.semantic_episode.confidence,EXCLUDED.confidence),
                updated_at=now()
            RETURNING semantic_episode_id
            """,
            episode_key, row["world_id"], row["timeline_id"], row["dag_node_id"],
            float(row["confidence"] or 0.5), EPISODE_RESOLVER_VERSION,
            json.dumps({"grouping": "world_timeline_dag_node"}),
        )
        ordinal_row = await db.fetchrow(
            """
            SELECT COALESCE(MAX(ordinal),-1)+1 AS ordinal
            FROM aios.semantic_episode_membership
            WHERE semantic_episode_id=$1
            """,
            episode["semantic_episode_id"],
        )
        await db.execute(
            """
            INSERT INTO aios.semantic_episode_membership (
                semantic_episode_id, semantic_event_id, ordinal,
                membership_confidence, assigned_by, status, meta
            )
            VALUES ($1,$2,$3,$4,$5,'active','{}'::jsonb)
            ON CONFLICT (semantic_episode_id,semantic_event_id) DO UPDATE
            SET status='active', updated_at=now()
            """,
            episode["semantic_episode_id"], row["semantic_event_id"],
            int(ordinal_row["ordinal"]), float(row["confidence"] or 0.5),
            EPISODE_RESOLVER_VERSION,
        )
        written += 1
    return written
