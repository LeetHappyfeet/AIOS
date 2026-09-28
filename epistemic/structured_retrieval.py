from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable
from uuid import UUID


class RetrievalScope(str, Enum):
    CHARACTER = "character"
    WORLD = "world"


class QueryKind(str, Enum):
    SEARCH = "search"
    EVIDENCE = "evidence"
    RELATION = "relation"
    HISTORY = "history"


@dataclass(frozen=True)
class KnowledgeQuery:
    scope: RetrievalScope
    kind: QueryKind = QueryKind.SEARCH
    text: str | None = None
    proposition_ids: tuple[UUID, ...] = ()
    entity_keys: tuple[str, ...] = ()
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    relation_types: tuple[str, ...] = ()
    max_hops: int = 1
    world_id: UUID | None = None
    timeline_id: UUID | None = None
    before_node_id: UUID | None = None
    after_node_id: UUID | None = None
    authorized_use: str = "reflection"
    limit: int = 10


class StructuredKnowledgeRetriever:
    """Exact, bounded retrieval over authoritative SQL coordinates.

    Semantic/vector retrieval discovers coordinates.  Once proposition/entity/node
    coordinates exist, this service answers graph, provenance and temporal
    questions without returning to fuzzy similarity search.  RDF remains a
    derived projection and is not an authority boundary.
    """

    def __init__(self, db: Any):
        self.db = db

    async def character_evidence(
        self, *, instance_ids: Iterable[UUID], proposition_ids: Iterable[UUID],
        authorized_use: str = "reflection", limit: int = 20,
    ) -> list[dict[str, Any]]:
        instances = list(instance_ids)
        propositions = list(proposition_ids)
        if not instances or not propositions:
            return []
        rows = await self.db.fetch(
            """
            SELECT kae.acquisition_id,kae.instance_id,kae.proposition_id,kae.claim_id,
                   kae.acquisition_mode,kae.epistemic_status,kae.confidence,
                   kae.dag_node_id,kae.created_at,
                   eaa.origin_kind,eaa.epistemic_mode,eaa.authority_state,
                   eaa.authority_rank,eaa.lineage_key,eaa.predicate_class,
                   eaa.authorized_uses,eaa.policy_version,
                   cbs.stance,cbs.positive_support,cbs.negative_support,
                   cbs.belief_confidence,cbs.evidence_count,
                   cbs.independent_evidence_count,cbs.resolved_through_node_id,
                   p.canonical_text,p.subject_norm,p.predicate_norm,p.object_norm,
                   ccr.timeline_id,ccr.world_id
            FROM aios.knowledge_acquisition_event kae
            JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
            JOIN aios.epistemic_authority_admission eaa
              ON eaa.acquisition_id=kae.acquisition_id
            LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=kae.claim_id
            LEFT JOIN aios.character_belief_state cbs
              ON cbs.instance_id=kae.instance_id AND cbs.atom_id=p.atom_id
            WHERE kae.instance_id=ANY($1::uuid[])
              AND kae.proposition_id=ANY($2::uuid[])
              AND $3::text=ANY(eaa.authorized_uses)
            ORDER BY array_position($1::uuid[],kae.instance_id),
                     kae.created_at DESC,kae.acquisition_id DESC
            LIMIT $4
            """,
            instances, propositions, authorized_use, max(1, min(int(limit), 100)),
        )
        return [{**dict(row), "retrieval_scope": "character", "retrieval_kind": "evidence"}
                for row in rows]

    async def relation(
        self, *, scope_key: str, instance_ids: Iterable[UUID] = (),
        seed_proposition_ids: Iterable[UUID] = (), seed_terms: Iterable[str] = (),
        relation_types: Iterable[str] = (), direction: str = "either",
        max_hops: int = 1, limit: int = 20,
    ) -> list[dict[str, Any]]:
        if direction not in {"outgoing", "incoming", "either"}:
            raise ValueError("direction must be outgoing, incoming, or either")
        hops = max(1, min(int(max_hops), 2))
        instances = list(instance_ids)
        proposition_ids = list(seed_proposition_ids)
        terms = [str(v).strip().lower() for v in seed_terms if str(v).strip()][:12]
        edge_types = [str(v).strip() for v in relation_types if str(v).strip()][:16]
        if not proposition_ids and not terms:
            return []
        rows = await self.db.fetch(
            """
            WITH RECURSIVE eligible AS (
                SELECT n.*
                FROM aios.semantic_topology_node n
                WHERE n.scope_key=$1
                  AND (cardinality($2::uuid[])=0
                       OR n.character_instance_id IS NULL
                       OR n.character_instance_id=ANY($2::uuid[]))
            ),
            seeds AS (
                SELECT topology_node_id
                FROM eligible n
                WHERE (cardinality($3::uuid[])>0 AND n.proposition_id=ANY($3::uuid[]))
                   OR (cardinality($4::text[])>0 AND EXISTS (
                        SELECT 1 FROM unnest($4::text[]) term
                        WHERE lower(COALESCE(n.label,'') || ' ' || n.node_key)
                              LIKE '%' || term || '%'))
                ORDER BY significance DESC,topology_node_id
                LIMIT 32
            ),
            walk(node_id,depth,path) AS (
                SELECT topology_node_id,0,ARRAY[topology_node_id] FROM seeds
                UNION ALL
                SELECT CASE WHEN e.parent_node_id=w.node_id THEN e.child_node_id
                            ELSE e.parent_node_id END,
                       w.depth+1,
                       w.path || CASE WHEN e.parent_node_id=w.node_id THEN e.child_node_id
                                     ELSE e.parent_node_id END
                FROM walk w
                JOIN aios.semantic_topology_edge e
                  ON e.scope_key=$1
                 AND (cardinality($5::text[])=0 OR e.edge_type=ANY($5::text[]))
                 AND (
                    ($6='outgoing' AND e.parent_node_id=w.node_id) OR
                    ($6='incoming' AND e.child_node_id=w.node_id) OR
                    ($6='either' AND (e.parent_node_id=w.node_id OR e.child_node_id=w.node_id))
                 )
                WHERE w.depth<$7
                  AND NOT (CASE WHEN e.parent_node_id=w.node_id THEN e.child_node_id
                                ELSE e.parent_node_id END = ANY(w.path))
            )
            SELECT DISTINCT ON (w.node_id)
                   w.depth,n.topology_node_id,n.node_type,n.node_key,n.label,
                   n.proposition_id,n.claim_id,n.assertion_id,n.acquisition_id,
                   n.dag_node_id,n.timeline_id,n.world_id,n.significance
            FROM walk w JOIN eligible n ON n.topology_node_id=w.node_id
            WHERE w.depth>0
            ORDER BY w.node_id,w.depth,n.significance DESC
            LIMIT $8
            """,
            scope_key, instances, proposition_ids, terms, edge_types,
            direction, hops, max(1, min(int(limit), 100)),
        )
        return [{**dict(row), "retrieval_kind": "relation", "scope_key": scope_key}
                for row in rows]

    async def history(
        self, *, timeline_id: UUID, anchor_node_id: UUID,
        direction: str = "before", limit: int = 10,
    ) -> list[dict[str, Any]]:
        if direction not in {"before", "after"}:
            raise ValueError("history direction must be before or after")
        anchor = await self.db.fetchrow(
            """SELECT created_at,event_time FROM aios.dag_node
               WHERE node_id=$1 AND timeline_id=$2""",
            anchor_node_id, timeline_id,
        )
        if not anchor:
            return []
        op = "<" if direction == "before" else ">"
        order = "DESC" if direction == "before" else "ASC"
        rows = await self.db.fetch(
            f"""SELECT node_id,timeline_id,event_id,kind,speaker_id,speaker_role,
                       recipient,message_text,event_time,created_at
                FROM aios.dag_node
                WHERE timeline_id=$1 AND (event_time,created_at,node_id) {op}
                      ($2,$3,$4)
                ORDER BY event_time {order},created_at {order},node_id {order}
                LIMIT $5""",
            timeline_id, anchor["event_time"], anchor["created_at"], anchor_node_id,
            max(1, min(int(limit), 50)),
        )
        return [{**dict(row), "retrieval_kind": "history", "direction": direction}
                for row in rows]
