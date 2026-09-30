"""Quarantine derived vectors without deleting observations or propositions."""
from __future__ import annotations

from qdrant_client.http import models as qm

from aios_app.db import Database
from .config import SemanticIndexConfig
from .service import _get_store


async def quarantine_ineligible_vectors_once(db: Database, cfg: SemanticIndexConfig) -> int:
    # Historical snapshots and decisions remain available for audit. Current
    # clusters containing withdrawn support stop contributing derived pivots.
    await db.execute("""
        UPDATE aios.semantic_cluster_candidate c SET status='stale',updated_at=now()
        WHERE c.status='candidate' AND EXISTS (
            SELECT 1 FROM aios.semantic_cluster_membership m
            WHERE m.cluster_id=c.cluster_id
              AND NOT aios.semantic_proposition_topology_admitted(m.proposition_id))
    """)
    await db.execute("""
        UPDATE aios.semantic_event_membership m SET status='superseded',updated_at=now(),
            meta=meta || '{"withdrawal_reason":"non_standalone_topology"}'::jsonb
        WHERE m.status='active' AND (
            NOT aios.semantic_proposition_topology_admitted(m.proposition_id)
            OR (m.claim_id IS NOT NULL AND NOT
                aios.semantic_claim_topology_admitted(m.claim_id)))
    """)
    await db.execute("""
        UPDATE aios.semantic_event e SET status='superseded',updated_at=now()
        WHERE e.status='active' AND EXISTS (
            SELECT 1 FROM aios.semantic_event_membership w
            WHERE w.semantic_event_id=e.semantic_event_id
              AND w.meta->>'withdrawal_reason'='non_standalone_topology')
          AND NOT EXISTS (
            SELECT 1 FROM aios.semantic_event_membership m
            WHERE m.semantic_event_id=e.semantic_event_id AND m.status='active')
    """)
    await db.execute("""
        UPDATE aios.semantic_episode_membership m SET status='superseded'
        WHERE m.status='active' AND EXISTS (SELECT 1 FROM aios.semantic_event e
            WHERE e.semantic_event_id=m.semantic_event_id AND e.status='superseded')
    """)
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
    for row in rows:
        key = str(row['proposition_id'])
        selector = qm.FilterSelector(filter=qm.Filter(must=[qm.FieldCondition(
            key='proposition_id', match=qm.MatchValue(value=key))]))
        for collection in (cfg.proposition_collection, cfg.epistemic_collection):
            store = _get_store(cfg, collection)
            store.client.delete(collection_name=collection, points_selector=selector, wait=True)
        await db.execute("""
            DELETE FROM aios.semantic_vector_index_state s
            WHERE s.qdrant_collection=ANY($2::text[]) AND (
                (s.object_type='proposition' AND s.object_key=$1::text)
                OR (s.object_type='character_knowledge' AND s.object_key LIKE '%:' || $1::text)
                OR (s.object_type='world_assertion' AND EXISTS (
                    SELECT 1 FROM aios.world_proposition_assertion wa
                    WHERE wa.proposition_id=$1 AND s.object_key='world:' || wa.assertion_id::text)))
        """, row['proposition_id'], [cfg.proposition_collection, cfg.epistemic_collection])
        await db.execute('DELETE FROM aios.semantic_structure_state WHERE proposition_id=$1', row['proposition_id'])
    return len(rows)
