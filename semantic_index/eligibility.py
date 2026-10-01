"""Quarantine derived vectors without deleting observations or propositions."""
from __future__ import annotations

import logging
import time

from qdrant_client.http import models as qm

from aios_app.db import Database
from .config import SemanticIndexConfig
from .service import _get_store, VECTOR_MUTATION_LOCK

logger = logging.getLogger("aios.semantic_index.eligibility")


async def quarantine_ineligible_vectors_once(db: Database, cfg: SemanticIndexConfig) -> int:
    started = time.monotonic()
    # Historical snapshots and decisions remain available for audit. Current
    # clusters containing withdrawn support stop contributing derived pivots.
    await db.execute("""
        WITH batch AS (
            SELECT c.cluster_id FROM aios.semantic_cluster_candidate c
            WHERE c.status='candidate' AND EXISTS (
                SELECT 1 FROM aios.semantic_cluster_membership m
                WHERE m.cluster_id=c.cluster_id
                  AND NOT aios.semantic_proposition_topology_admitted(m.proposition_id))
            ORDER BY c.cluster_id LIMIT $1
        )
        UPDATE aios.semantic_cluster_candidate c SET status='stale',updated_at=now()
        FROM batch WHERE c.cluster_id=batch.cluster_id
    """, cfg.batch_size)
    await db.execute("""
        WITH batch AS (
            SELECT m.ctid FROM aios.semantic_event_membership m
            WHERE m.status='active' AND (
                NOT aios.semantic_proposition_topology_admitted(m.proposition_id)
                OR (m.claim_id IS NOT NULL AND NOT
                    aios.semantic_claim_topology_admitted(m.claim_id)))
            LIMIT $1
        )
        UPDATE aios.semantic_event_membership m SET status='superseded',updated_at=now(),
            meta=meta || '{"withdrawal_reason":"non_standalone_topology"}'::jsonb
        WHERE m.ctid IN (SELECT ctid FROM batch)
    """, cfg.batch_size)
    await db.execute("""
        WITH batch AS (
            SELECT e.semantic_event_id FROM aios.semantic_event e
            WHERE e.status='active' AND EXISTS (
            SELECT 1 FROM aios.semantic_event_membership w
            WHERE w.semantic_event_id=e.semantic_event_id
              AND w.meta->>'withdrawal_reason'='non_standalone_topology')
              AND NOT EXISTS (
            SELECT 1 FROM aios.semantic_event_membership m
            WHERE m.semantic_event_id=e.semantic_event_id AND m.status='active')
            ORDER BY e.semantic_event_id LIMIT $1
        )
        UPDATE aios.semantic_event e SET status='superseded',updated_at=now()
        FROM batch WHERE e.semantic_event_id=batch.semantic_event_id
    """, cfg.batch_size)
    await db.execute("""
        WITH batch AS (
            SELECT m.ctid FROM aios.semantic_episode_membership m
            WHERE m.status='active' AND EXISTS (SELECT 1 FROM aios.semantic_event e
                WHERE e.semantic_event_id=m.semantic_event_id AND e.status='superseded')
            LIMIT $1
        )
        UPDATE aios.semantic_episode_membership m SET status='superseded'
        WHERE m.ctid IN (SELECT ctid FROM batch)
    """, cfg.batch_size)
    # Node/edge deletion triggers write durable RDF deletion deltas. Only
    # derived topology is removed; its original evidence stays in SQL.
    await db.execute("""
        DELETE FROM aios.semantic_topology_node n WHERE n.topology_node_id IN (
            SELECT x.topology_node_id FROM aios.semantic_topology_node x
            WHERE (x.proposition_id IS NOT NULL AND NOT
                aios.semantic_proposition_topology_admitted(x.proposition_id))
               OR (x.claim_id IS NOT NULL AND NOT
                aios.semantic_claim_topology_admitted(x.claim_id))
               OR (x.node_key LIKE 'semantic_event:%' AND EXISTS (
                   SELECT 1 FROM aios.semantic_event e
                   WHERE e.semantic_event_id::text=x.meta->>'semantic_event_id'
                     AND NOT EXISTS (SELECT 1 FROM aios.semantic_event_membership m
                         WHERE m.semantic_event_id=e.semantic_event_id AND m.status='active')))
            ORDER BY x.topology_node_id LIMIT $1
        )
    """, cfg.batch_size)
    # Index receipts identify vectors we own; a failed delete leaves receipts
    # intact so the next pass retries. Filter deletion also removes owner copies.
    rows = await db.fetch("""
        SELECT DISTINCT p.proposition_id
        FROM aios.proposition p
        JOIN aios.semantic_vector_index_state s ON
            (s.object_type='proposition' AND s.object_key=p.proposition_id::text)
            OR (s.object_type='character_knowledge' AND s.object_key LIKE '%:' || p.proposition_id::text)
            OR (s.object_type='world_assertion' AND EXISTS (
                SELECT 1 FROM aios.world_proposition_assertion wa
                WHERE wa.proposition_id=p.proposition_id AND s.object_key='world:' || wa.assertion_id::text))
        WHERE s.qdrant_collection=ANY($1::text[])
          AND NOT aios.semantic_proposition_topology_eligible(p.proposition_id)
        ORDER BY p.proposition_id LIMIT $2
    """, [cfg.proposition_collection, cfg.epistemic_collection], cfg.batch_size)
    if not rows:
        return 0

    async with db.connection() as con:
        async with con.transaction():
            # Cleanup yields immediately to indexing, and rechecks eligibility
            # under the shared write lock before deleting any points/receipts.
            if not await con.fetchval("SELECT pg_try_advisory_xact_lock($1)", VECTOR_MUTATION_LOCK):
                return 0
            rows = await con.fetch("""SELECT proposition_id FROM aios.proposition
                WHERE proposition_id=ANY($1::uuid[])
                  AND NOT aios.semantic_proposition_topology_eligible(proposition_id)""",
                [r["proposition_id"] for r in rows])
            if not rows:
                return 0
            keys = [str(row['proposition_id']) for row in rows]
            proposition_ids = [row['proposition_id'] for row in rows]
            selector = qm.FilterSelector(filter=qm.Filter(must=[qm.FieldCondition(
                key='proposition_id', match=qm.MatchAny(any=keys))]))
            for collection in (cfg.proposition_collection, cfg.epistemic_collection):
                store = _get_store(cfg, collection)
                store.client.delete(collection_name=collection, points_selector=selector, wait=True)

            # One SQL delete clears the index receipts for all collection copies only
            # after Qdrant has acknowledged both collection deletes.
            await con.execute("""
                DELETE FROM aios.semantic_vector_index_state s
                WHERE s.qdrant_collection=ANY($2::text[]) AND (
                    (s.object_type='proposition' AND s.object_key=ANY($1::text[]))
                    OR (s.object_type='character_knowledge'
                        AND split_part(s.object_key,':',2)=ANY($1::text[]))
                    OR (s.object_type='world_assertion' AND EXISTS (
                        SELECT 1 FROM aios.world_proposition_assertion wa
                        WHERE wa.proposition_id=ANY($3::uuid[])
                          AND s.object_key='world:' || wa.assertion_id::text)))
            """, keys, [cfg.proposition_collection, cfg.epistemic_collection], proposition_ids)
            await con.execute("""
                DELETE FROM aios.semantic_structure_state WHERE proposition_id=ANY($1::uuid[])
            """, proposition_ids)
    logger.info("Quarantined %d ineligible propositions in %.2fs",
                len(rows), time.monotonic() - started)
    return len(rows)
