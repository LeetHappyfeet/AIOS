from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from aios_app.db import Database
from aios_app.hud.context import HUDContext
from aios_app.epistemic.relevance import CognitiveRelevanceScorer
from aios_app.epistemic.event_projection import project_semantic_event
from aios_app.epistemic.episode_projection import project_semantic_episode
from aios_app.hud.singleflight import AsyncSingleFlight
from aios_app.semantic_index.query import SemanticQueryService


logger = logging.getLogger("aios.hud.retrieval")
_WORD_RE = re.compile(r"[a-z0-9_'-]+")
SEMANTIC_SEED_WAIT_SECONDS = 0.25
TOPOLOGY_SQL_TIMEOUT_SECONDS = 2.0
TOPOLOGY_FALLBACK_TIMEOUT_SECONDS = 1.0
MAX_FOCUS_TERMS = 12
MAX_TOPOLOGY_SEEDS = 64


@dataclass(frozen=True)
class RetrievalPolicy:
    name: str
    claim_kinds: tuple[str, ...]
    max_hops: int
    limit: int
    retain_topic_history: bool = False


POLICIES = {
    "memory": RetrievalPolicy("memory", ("MEMORY",), 3, 60, True),
    "belief": RetrievalPolicy(
        "belief", ("BELIEF", "TRAIT", "STATE", "CONCEPT"), 2, 60, False
    ),
    "goal": RetrievalPolicy("goal", ("GOAL",), 2, 30, False),
    "event": RetrievalPolicy("event", ("EVENT",), 2, 40, True),
    "rule": RetrievalPolicy("rule", ("RULE",), 1, 30, False),
}


def _focus_terms(*values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        for word in _WORD_RE.findall(str(value or "").lower()):
            if len(word) < 3 or word in seen:
                continue
            seen.add(word)
            result.append(word)
            if len(result) >= MAX_FOCUS_TERMS:
                return result
    return result


_RETRIEVAL_SQL = """
WITH RECURSIVE
eligible_nodes AS (
    SELECT n.*
    FROM aios.semantic_topology_node n
    WHERE n.scope_key=$1
      AND (
          n.character_instance_id IS NULL
          OR n.character_instance_id = ANY($2::uuid[])
          OR (n.node_type='INSTANCE' AND n.node_key = ANY($3::text[]))
      )
),
seed_candidates AS (
    SELECT
        topology_node_id,
        significance,
        CASE
            WHEN node_type='INSTANCE' AND node_key=$4 THEN 0
            WHEN cardinality($6::uuid[]) > 0
                 AND proposition_id = ANY($6::uuid[]) THEN 1
            ELSE 2
        END AS seed_rank
    FROM eligible_nodes
    WHERE (node_type='INSTANCE' AND node_key=$4)
       OR (
            cardinality($5::text[]) > 0
            AND EXISTS (
                SELECT 1
                FROM unnest($5::text[]) term
                WHERE lower(COALESCE(label,'')) LIKE '%' || term || '%'
                   OR lower(node_key) LIKE '%' || term || '%'
            )
       )
       OR (
            cardinality($6::uuid[]) > 0
            AND proposition_id = ANY($6::uuid[])
       )
),
seeds AS (
    SELECT topology_node_id
    FROM seed_candidates
    ORDER BY seed_rank, significance DESC NULLS LAST, topology_node_id
    LIMIT 64
),
walk(topology_node_id, depth) AS (
    SELECT s.topology_node_id, 0
    FROM seeds s

    UNION

    SELECT
        CASE
            WHEN e.parent_node_id=w.topology_node_id THEN e.child_node_id
            ELSE e.parent_node_id
        END,
        w.depth + 1
    FROM walk w
    JOIN aios.semantic_topology_edge e
      ON e.scope_key=$1
     AND (
         e.parent_node_id=w.topology_node_id
         OR e.child_node_id=w.topology_node_id
     )
    JOIN eligible_nodes next_node
      ON next_node.topology_node_id = CASE
          WHEN e.parent_node_id=w.topology_node_id THEN e.child_node_id
          ELSE e.parent_node_id
      END
    WHERE w.depth < $7
),
nearest AS (
    SELECT
        topology_node_id,
        MIN(depth) AS topology_depth,
        MIN(depth)::double precision AS topology_cost
    FROM walk
    GROUP BY topology_node_id
),
topology_props AS (
    SELECT
        n.proposition_id,
        MIN(ne.topology_depth) AS topology_depth,
        MIN(ne.topology_cost) AS topology_cost,
        MAX(n.significance) AS topology_significance
    FROM nearest ne
    JOIN eligible_nodes n ON n.topology_node_id=ne.topology_node_id
    WHERE n.proposition_id IS NOT NULL
    GROUP BY n.proposition_id
),
belief_owned AS (
    SELECT DISTINCT ON (ck.proposition_id)
        ck.instance_id,
        ck.proposition_id,
        ck.atom_id,
        NULL::uuid AS evidence_claim_id,
        ck.epistemic_status,
        ck.confidence,
        ck.acquisition_mode,
        ck.source_entity_id,
        ck.first_node_id,
        ck.last_node_id,
        ck.first_acquired_at,
        ck.updated_at,
        ck.base_confidence,
        ck.attention_weight,
        ck.trust_weight,
        ck.compatibility_weight,
        ck.retention_weight,
        ck.salience_weight,
        ck.effective_confidence,
        array_position($2::uuid[], ck.instance_id) AS instance_depth
    FROM aios.character_active_proposition_knowledge ck
    WHERE ck.instance_id = ANY($2::uuid[])
      AND EXISTS (
          SELECT 1
          FROM aios.knowledge_acquisition_event kae
          LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
          LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
          LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
          LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id
          LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
          WHERE kae.instance_id=ck.evidence_instance_id
            AND kae.proposition_id=ck.proposition_id
            AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
      )
    ORDER BY ck.proposition_id,
             array_position($2::uuid[], ck.instance_id),
             ck.updated_at DESC
),
episodic_owned AS (
    -- EVENT/MEMORY evidence is experiential knowledge, not a belief winner.
    -- Read it from admitted character acquisition rather than requiring the
    -- character_belief_state projection to resolve positive.
    SELECT DISTINCT ON (cpk.proposition_id)
        cpk.instance_id,
        cpk.proposition_id,
        p.atom_id,
        kae.claim_id AS evidence_claim_id,
        cpk.epistemic_status,
        cpk.confidence,
        cpk.acquisition_mode,
        cpk.source_entity_id,
        cpk.first_node_id,
        cpk.last_node_id,
        cpk.first_acquired_at,
        cpk.updated_at,
        cpk.base_confidence,
        cpk.attention_weight,
        cpk.trust_weight,
        cpk.compatibility_weight,
        cpk.retention_weight,
        cpk.salience_weight,
        cpk.effective_confidence,
        array_position($2::uuid[], cpk.instance_id) AS instance_depth
    FROM aios.character_proposition_knowledge cpk
    JOIN aios.proposition p ON p.proposition_id=cpk.proposition_id
    JOIN aios.knowledge_acquisition_event kae
      ON kae.instance_id=cpk.instance_id
     AND kae.proposition_id=cpk.proposition_id
    JOIN aios.observation obs ON obs.proposition_id=cpk.proposition_id
    JOIN aios.claim_context_resolution ccr ON ccr.claim_id=obs.claim_id
    LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
    LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
    LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
    LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id
    LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
    WHERE cpk.instance_id = ANY($2::uuid[])
      AND ccr.claim_kind IN ('EVENT','MEMORY')
      AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
    ORDER BY cpk.proposition_id,
             array_position($2::uuid[], cpk.instance_id),
             cpk.updated_at DESC,
             kae.created_at DESC,
             kae.acquisition_id DESC
),
owned AS (
    SELECT * FROM belief_owned
    UNION
    SELECT * FROM episodic_owned
),
classified AS (
    SELECT
        o.*,
        p.topic_key,
        p.canonical_text,
        p.subject_norm,
        p.predicate_norm,
        p.object_norm,
        p.polarity,
        p.modality,
        COALESCE(ctx.claim_kind, 'BELIEF') AS claim_kind,
        ctx.predicate_family,
        ctx.world_id AS source_world_id,
        ctx.dag_node_id AS source_node_id,
        ctx.event_time AS occurrence_time,
        tp.topology_depth,
        tp.topology_cost,
        tp.topology_significance
    FROM topology_props tp
    JOIN owned o ON o.proposition_id=tp.proposition_id
    JOIN aios.proposition p ON p.proposition_id=o.proposition_id
    LEFT JOIN LATERAL (
        SELECT ccr.claim_kind, ccr.predicate_family,
               ccr.world_id, ccr.dag_node_id, dn.event_time
        FROM aios.observation obs
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=obs.claim_id
        LEFT JOIN aios.dag_node dn ON dn.node_id=ccr.dag_node_id
        WHERE obs.proposition_id=p.proposition_id
          AND (
              ccr.character_instance_id IS NULL
              OR ccr.character_instance_id = ANY($2::uuid[])
          )
        ORDER BY
            -- A proposition can be emitted by several claims with different
            -- semantic kinds. Prefer the claim that actually produced this
            -- acquisition; otherwise a STATE observation can borrow an EVENT
            -- label from another occurrence of the same proposition and leak
            -- into ACTIVE MEMORY.
            (obs.claim_id IS NOT DISTINCT FROM o.evidence_claim_id) DESC,
            array_position($2::uuid[], ccr.character_instance_id) NULLS LAST,
            ccr.resolved_at DESC
        LIMIT 1
    ) ctx ON true
    WHERE COALESCE(ctx.claim_kind, 'BELIEF') = ANY($8::text[])
),
topic_ranked AS (
    SELECT c.*,
           row_number() OVER (
               PARTITION BY c.atom_id
               ORDER BY
                   c.instance_depth,
                   c.updated_at DESC,
                   c.effective_confidence DESC NULLS LAST,
                   c.topology_cost,
                   c.proposition_id
           ) AS topic_recency_rank
    FROM classified c
)
SELECT *
FROM topic_ranked
WHERE $9::boolean OR topic_recency_rank=1
ORDER BY
    topology_cost,
    topology_depth,
    topology_significance DESC,
    instance_depth,
    updated_at DESC
LIMIT $10
"""


class TopologyRetriever:
    """Branch-safe retrieval over derived semantic topology.

    The recursive walk intentionally carries only node id and depth and uses
    UNION (not UNION ALL). PostgreSQL therefore deduplicates the same node at
    the same depth during the recursion instead of materializing every distinct
    path and collapsing them only after the graph has exploded.
    """

    def __init__(self, db: Database):
        self.db = db
        self.semantic = SemanticQueryService()
        self._semantic_seed_cache: dict[tuple[str, str, tuple[str, ...]], dict[str, float]] = {}
        self._semantic_seed_flights: AsyncSingleFlight[
            tuple[str, str, tuple[str, ...]], dict[str, float]
        ] = AsyncSingleFlight()
        self._semantic_seed_deferred: set[tuple[str, str, tuple[str, ...]]] = set()

    async def _query_semantic_seed_propositions(
        self,
        context: HUDContext,
        *,
        query_text: str,
        cache_key: tuple[str, str, tuple[str, ...]],
    ) -> dict[str, float]:
        started = time.perf_counter()
        try:
            hits = await asyncio.to_thread(
                self.semantic.search_epistemic,
                query_text,
                character_id=context.character_id,
                instance_ids=(context.cognitive_instance_ids or context.lineage_instance_ids),
            )
            proposition_scores: dict[str, float] = {}
            for _, similarity, payload in hits:
                proposition_id = str(payload.get("proposition_id") or "")
                if proposition_id:
                    proposition_scores[proposition_id] = max(
                        proposition_scores.get(proposition_id, 0.0), float(similarity)
                    )
            self._semantic_seed_cache[cache_key] = proposition_scores
            if len(self._semantic_seed_cache) > 64:
                self._semantic_seed_cache.pop(next(iter(self._semantic_seed_cache)))
            return proposition_scores
        except Exception as exc:
            logger.debug(
                "Semantic seed lookup unavailable; using topology/lexical fallback: %s", exc
            )
            self._semantic_seed_cache[cache_key] = {}
            return {}
        finally:
            self._semantic_seed_deferred.discard(cache_key)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if elapsed_ms >= 500.0:
                logger.warning("HUD semantic seed lookup took %.1f ms", elapsed_ms)
            else:
                logger.debug("HUD semantic seed lookup took %.1f ms", elapsed_ms)

    async def _semantic_seed_propositions(
        self,
        context: HUDContext,
        *,
        focus_text: str,
        goals: Iterable[Any],
    ) -> dict[str, float]:
        query_text = " ".join(
            part
            for part in (focus_text, " ".join(str(goal) for goal in goals))
            if part
        ).strip()
        if not query_text:
            return {}
        lineage = tuple(str(value) for value in (context.cognitive_instance_ids or context.lineage_instance_ids))
        cache_key = (str(context.character_id), query_text, lineage)
        cached = self._semantic_seed_cache.get(cache_key)
        if cached is not None:
            return cached
        if cache_key in self._semantic_seed_deferred:
            return {}
        try:
            return await asyncio.wait_for(
                self._semantic_seed_flights.run(
                    cache_key,
                    lambda: self._query_semantic_seed_propositions(
                        context, query_text=query_text, cache_key=cache_key
                    ),
                ),
                timeout=SEMANTIC_SEED_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._semantic_seed_deferred.add(cache_key)
            logger.debug(
                "HUD semantic seed exceeded %.0f ms budget; using lexical/topology fallback",
                SEMANTIC_SEED_WAIT_SECONDS * 1000.0,
            )
            return {}

    async def _canonical_event_memberships(
        self, proposition_ids: list[Any]
    ) -> dict[Any, dict[str, Any]]:
        """Map retrieved EVENT propositions onto their canonical occurrence.

        Pairwise SAME_EVENT evidence stays below this boundary. Retrieval exposes
        one recall candidate per canonical semantic event while retaining member
        proposition ids as provenance.
        """
        if not proposition_ids:
            return {}
        rows = await self.db.fetch(
            """
            SELECT
                sem.semantic_event_id,
                sem.confidence AS event_confidence,
                sem.world_id,
                sem.timeline_id,
                sem.dag_node_id,
                m.proposition_id,
                (
                    SELECT array_agg(m2.proposition_id ORDER BY m2.proposition_id)
                    FROM aios.semantic_event_membership m2
                    WHERE m2.semantic_event_id=sem.semantic_event_id
                      AND m2.status='active'
                ) AS member_proposition_ids,
                (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'proposition_id', p2.proposition_id,
                            'subject_norm', p2.subject_norm,
                            'predicate_norm', p2.predicate_norm,
                            'object_norm', p2.object_norm,
                            'text', p2.canonical_text
                        )
                        ORDER BY m2.created_at, p2.proposition_id
                    )
                    FROM aios.semantic_event_membership m2
                    JOIN aios.proposition p2 ON p2.proposition_id=m2.proposition_id
                    WHERE m2.semantic_event_id=sem.semantic_event_id
                      AND m2.status='active'
                ) AS member_rows
            FROM aios.semantic_event_membership m
            JOIN aios.semantic_event sem
              ON sem.semantic_event_id=m.semantic_event_id
             AND sem.status='active'
            WHERE m.status='active'
              AND m.proposition_id = ANY($1::uuid[])
            """,
            proposition_ids,
        )
        return {
            row["proposition_id"]: {
                "semantic_event_id": row["semantic_event_id"],
                "event_confidence": row["event_confidence"],
                "world_id": row["world_id"],
                "timeline_id": row["timeline_id"],
                "dag_node_id": row["dag_node_id"],
                "member_proposition_ids": list(row["member_proposition_ids"] or []),
                "member_rows": list(row["member_rows"] or []),
            }
            for row in rows
        }

    @staticmethod
    def _collapse_canonical_events(
        items: list[dict[str, Any]],
        event_by_proposition: dict[Any, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Promote member propositions into first-class canonical event memories."""
        passthrough: list[dict[str, Any]] = []
        grouped: dict[Any, list[dict[str, Any]]] = {}
        for item in items:
            event = event_by_proposition.get(item.get("proposition_id"))
            if not event or str(item.get("claim_kind") or "").upper() != "EVENT":
                passthrough.append(item)
                continue
            grouped.setdefault(event["semantic_event_id"], []).append(item)

        for event_id, members in grouped.items():
            event = dict(event_by_proposition[members[0]["proposition_id"]])
            event["semantic_event_id"] = event_id
            full_members = [dict(row) for row in event.get("member_rows") or []]
            retrieved_by_id = {member.get("proposition_id"): member for member in members}
            for row in full_members:
                retrieved = retrieved_by_id.get(row.get("proposition_id"))
                if retrieved:
                    row["relevance"] = dict(retrieved.get("relevance") or {})
            projection = project_semantic_event(event, full_members or members)

            # The event, not a member proposition, is the recall candidate. Keep
            # the strongest member only as a compatibility/provenance carrier.
            representative = max(
                members,
                key=lambda item: (
                    float((item.get("relevance") or {}).get("total") or 0.0),
                    len(str(item.get("text") or "")),
                    str(item.get("proposition_id") or ""),
                ),
            )
            candidate = dict(representative)
            candidate["text"] = projection["text"] or representative.get("text") or ""
            candidate["semantic_event_id"] = event_id
            candidate["semantic_event_confidence"] = projection["confidence"]
            candidate["semantic_event_members"] = projection["member_proposition_ids"]
            candidate["semantic_event_dag_node_id"] = projection["dag_node_id"]
            candidate["event_projection"] = projection
            candidate["retrieval_reason"] = "canonical_semantic_event"

            member_relevance = [dict(member.get("relevance") or {}) for member in members]
            best_total = max(float(r.get("total") or 0.0) for r in member_relevance)
            best_vector = projection.get("best_vector_similarity")
            relevance = dict(candidate.get("relevance") or {})
            if best_vector is not None:
                relevance["vector_similarity"] = best_vector
                relevance["vector_semantic"] = 1.8 * max(0.0, min(1.0, float(best_vector)))
            relevance["event_member_count"] = len(members)
            relevance["event_confidence"] = projection["confidence"]
            # Member count is deliberately logarithmic and tightly bounded:
            # extraction verbosity must not make an event important by itself.
            support_bonus = min(0.16, 0.06 * math.log1p(max(0, len(members) - 1)))
            relevance["event_support_bonus"] = round(support_bonus, 6)
            relevance["total"] = round(
                best_total
                + 0.20 * max(0.0, min(1.0, projection["confidence"]))
                + support_bonus,
                6,
            )
            candidate["relevance"] = relevance
            passthrough.append(candidate)
        return passthrough

    async def _episode_memberships(
        self, semantic_event_ids: list[Any]
    ) -> dict[Any, dict[str, Any]]:
        if not semantic_event_ids:
            return {}
        rows = await self.db.fetch(
            """
            SELECT em.semantic_event_id, ep.semantic_episode_id, ep.world_id,
                   ep.timeline_id, ep.confidence AS episode_confidence,
                   em.ordinal
            FROM aios.semantic_episode_membership em
            JOIN aios.semantic_episode ep
              ON ep.semantic_episode_id=em.semantic_episode_id
             AND ep.status='active'
            WHERE em.status='active'
              AND em.semantic_event_id=ANY($1::uuid[])
            """,
            semantic_event_ids,
        )
        return {
            row["semantic_event_id"]: {
                "semantic_episode_id": row["semantic_episode_id"],
                "world_id": row["world_id"],
                "timeline_id": row["timeline_id"],
                "episode_confidence": row["episode_confidence"],
                "ordinal": row["ordinal"],
            }
            for row in rows
        }

    @staticmethod
    def _collapse_semantic_episodes(
        items: list[dict[str, Any]],
        episode_by_event: dict[Any, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        passthrough: list[dict[str, Any]] = []
        grouped: dict[Any, list[dict[str, Any]]] = {}
        for item in items:
            event_id = item.get("semantic_event_id")
            episode = episode_by_event.get(event_id)
            if not event_id or not episode:
                passthrough.append(item)
                continue
            grouped.setdefault(episode["semantic_episode_id"], []).append(item)

        for episode_id, members in grouped.items():
            episode = dict(episode_by_event[members[0]["semantic_event_id"]])
            event_rows = []
            for member in members:
                projection = dict(member.get("event_projection") or {})
                event_rows.append({
                    "semantic_event_id": member.get("semantic_event_id"),
                    "event_confidence": member.get("semantic_event_confidence"),
                    "episode_ordinal": episode_by_event[member["semantic_event_id"]]["ordinal"],
                    "members": list(projection.get("members") or []),
                })
            projection = project_semantic_episode(episode, event_rows)
            representative = max(
                members,
                key=lambda item: float((item.get("relevance") or {}).get("total") or 0.0),
            )
            candidate = dict(representative)
            candidate["text"] = projection.get("text") or candidate.get("text") or ""
            candidate["semantic_episode_id"] = episode_id
            candidate["semantic_episode_events"] = projection.get("semantic_event_ids") or []
            candidate["episode_projection"] = projection
            candidate["retrieval_reason"] = "semantic_episode"
            relevance = dict(candidate.get("relevance") or {})
            relevance["episode_event_count"] = len(members)
            relevance["total"] = max(
                float((member.get("relevance") or {}).get("total") or 0.0)
                for member in members
            )
            candidate["relevance"] = relevance
            passthrough.append(candidate)
        return passthrough

    async def _anchor_context(
        self, context: HUDContext, proposition_ids: list[Any]
    ) -> dict[Any, dict[str, Any]]:
        if not proposition_ids:
            return {}
        rows = await self.db.fetch(
            """
            SELECT
                sae.proposition_id, sae.relationship_type, sae.world_id,
                sae.source_scope_key, sae.target_scope_key, sae.target_node_id,
                sae.confidence, sae.inference_source, sae.inference_status,
                sae.character_instance_id,
                target.node_type AS target_type,
                target.label AS target_label,
                neighbor.topology_node_id AS context_node_id,
                neighbor.node_type AS context_node_type,
                neighbor.label AS context_label,
                e.edge_type AS context_edge_type
            FROM aios.semantic_anchor_edge sae
            JOIN aios.semantic_topology_node target
              ON target.topology_node_id=sae.target_node_id
            LEFT JOIN aios.semantic_topology_edge e
              ON e.scope_key=sae.target_scope_key
             AND (e.parent_node_id=sae.target_node_id OR e.child_node_id=sae.target_node_id)
            LEFT JOIN aios.semantic_topology_node neighbor
              ON neighbor.topology_node_id=CASE
                  WHEN e.parent_node_id=sae.target_node_id THEN e.child_node_id
                  ELSE e.parent_node_id
              END
             AND neighbor.scope_key=sae.target_scope_key
            WHERE sae.source_scope_key=$1
              AND sae.character_id=$2
              AND sae.character_instance_id = ANY($3::uuid[])
              AND sae.proposition_id = ANY($4::uuid[])
            ORDER BY sae.confidence DESC, e.significance DESC NULLS LAST,
                     neighbor.significance DESC NULLS LAST
            """,
            f"char:{context.character_id}",
            context.character_id,
            list((context.cognitive_instance_ids or context.lineage_instance_ids)),
            proposition_ids,
        )
        anchors: dict[Any, dict[str, Any]] = {}
        for row in rows:
            proposition_id = row["proposition_id"]
            entry = anchors.get(proposition_id)
            if entry is None:
                world_id = row["world_id"]
                entry = {
                    "relationship": row["relationship_type"],
                    "world_id": world_id,
                    "source_scope": row["source_scope_key"],
                    "target_scope": row["target_scope_key"],
                    "target_node_id": row["target_node_id"],
                    "target_type": row["target_type"],
                    "target_label": row["target_label"],
                    "confidence": row["confidence"],
                    "inference_source": row["inference_source"],
                    "inference_status": row["inference_status"],
                    "world_visible": context.world_visible(world_id),
                    "world_context": [],
                }
                anchors[proposition_id] = entry
            if not entry["world_visible"]:
                continue
            context_node_id = row["context_node_id"]
            if context_node_id is None or len(entry["world_context"]) >= 8:
                continue
            if any(item["node_id"] == context_node_id for item in entry["world_context"]):
                continue
            entry["world_context"].append(
                {
                    "node_id": context_node_id,
                    "node_type": row["context_node_type"],
                    "label": row["context_label"],
                    "edge_type": row["context_edge_type"],
                }
            )
        return anchors

    async def _fetch_rows(
        self,
        *,
        scope_key: str,
        lineage_ids: list[Any],
        lineage_keys: list[str],
        instance_id: str,
        terms: list[str],
        semantic_seed_ids: list[str],
        hops: int,
        policy: RetrievalPolicy,
        row_limit: int,
        timeout: float,
    ) -> list[Any]:
        return await asyncio.wait_for(
            self.db.fetch(
                _RETRIEVAL_SQL,
                scope_key,
                lineage_ids,
                lineage_keys,
                instance_id,
                terms,
                semantic_seed_ids,
                hops,
                list(policy.claim_kinds),
                bool(policy.retain_topic_history),
                row_limit,
            ),
            timeout=timeout,
        )

    async def retrieve_character_knowledge(
        self,
        context: HUDContext,
        scorer: CognitiveRelevanceScorer,
        *,
        mode: str,
        focus_text: str = "",
        goals: Iterable[Any] = (),
        max_hops: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        started = time.perf_counter()
        policy = POLICIES.get(mode)
        if policy is None:
            raise ValueError(f"unknown retrieval mode '{mode}'")

        hops = max(0, min(int(max_hops if max_hops is not None else policy.max_hops), 6))
        row_limit = max(1, min(int(limit if limit is not None else policy.limit), 250))
        scope_key = f"char:{context.character_id}"
        lineage_ids = list((context.cognitive_instance_ids or context.lineage_instance_ids))
        lineage_keys = [str(value) for value in (context.cognitive_instance_ids or context.lineage_instance_ids)]
        terms = _focus_terms(focus_text, " ".join(str(goal) for goal in goals))

        semantic_started = time.perf_counter()
        semantic_seed_scores = await self._semantic_seed_propositions(
            context, focus_text=focus_text, goals=goals
        )
        semantic_ms = (time.perf_counter() - semantic_started) * 1000.0

        topology_started = time.perf_counter()
        topology_fallback = False
        try:
            rows = await self._fetch_rows(
                scope_key=scope_key,
                lineage_ids=lineage_ids,
                lineage_keys=lineage_keys,
                instance_id=str(context.instance_id),
                terms=terms,
                semantic_seed_ids=list(semantic_seed_scores),
                hops=hops,
                policy=policy,
                row_limit=row_limit,
                timeout=TOPOLOGY_SQL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            topology_fallback = True
            logger.warning(
                "HUD topology retrieval mode=%s exceeded %.1fs; retrying seed-only fallback",
                mode,
                TOPOLOGY_SQL_TIMEOUT_SECONDS,
            )
            try:
                rows = await self._fetch_rows(
                    scope_key=scope_key,
                    lineage_ids=lineage_ids,
                    lineage_keys=lineage_keys,
                    instance_id=str(context.instance_id),
                    terms=terms,
                    semantic_seed_ids=list(semantic_seed_scores),
                    hops=0,
                    policy=policy,
                    row_limit=row_limit,
                    timeout=TOPOLOGY_FALLBACK_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "HUD topology seed-only fallback mode=%s exceeded %.1fs; returning no rows",
                    mode,
                    TOPOLOGY_FALLBACK_TIMEOUT_SECONDS,
                )
                rows = []
        topology_ms = (time.perf_counter() - topology_started) * 1000.0

        anchor_started = time.perf_counter()
        anchor_by_proposition = await self._anchor_context(
            context, [row["proposition_id"] for row in rows]
        )
        anchor_ms = (time.perf_counter() - anchor_started) * 1000.0

        result: list[dict[str, Any]] = []
        for rank, row in enumerate(rows):
            item = dict(row)
            item["text"] = item.pop("canonical_text")
            # Preserve the cognition route as provenance. Downstream section
            # routing can assert that only memory/event retrieval contributes
            # to ACTIVE MEMORY instead of trusting a potentially ambiguous
            # proposition-level label.
            item["retrieval_mode"] = mode
            anchor = anchor_by_proposition.get(item["proposition_id"])
            if anchor:
                item["anchor"] = {
                    key: value for key, value in anchor.items() if key != "world_context"
                }
                item["world_context"] = list(anchor.get("world_context") or [])
            candidate_world_id = (
                anchor.get("world_id")
                if anchor and anchor.get("world_id") is not None
                else item.get("source_world_id") or context.world_id
            )
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=(
                    f"{item.get('topic_key','')} {item.get('subject_norm','')} "
                    f"{item.get('predicate_norm','')} {item.get('object_norm','')} "
                    f"{item.get('text','')}"
                ),
                candidate_world_id=candidate_world_id,
                candidate_entity_id=item.get("source_entity_id"),
                epistemic_status=item.get("epistemic_status"),
                confidence=item.get("effective_confidence") or item.get("confidence"),
                updated_at=(
                    (item.get("occurrence_time") or item.get("first_acquired_at") or item.get("updated_at"))
                    if str(item.get("claim_kind") or "").upper() in {"EVENT", "MEMORY"}
                    else item.get("first_acquired_at") or item.get("updated_at")
                ),
                causal_distance=item.get("topology_depth"),
                semantic_similarity=semantic_seed_scores.get(str(item.get("proposition_id"))),
            )
            topology_bonus = (
                0.45 / (1.0 + float(item.get("topology_cost") or 0.0))
                + 0.25 * float(item.get("topology_significance") or 0.0)
            )
            item["topology"] = {
                "depth": int(item.get("topology_depth") or 0),
                "cost": float(item.get("topology_cost") or 0.0),
                "significance": float(item.get("topology_significance") or 0.0),
                "topic_recency_rank": int(item.get("topic_recency_rank") or 1),
                "historical": int(item.get("topic_recency_rank") or 1) > 1,
                "instance_depth": int(item.get("instance_depth") or 0),
                "fallback": topology_fallback,
            }
            item["relevance"] = score.as_dict()
            item["relevance"]["vector_similarity"] = semantic_seed_scores.get(str(item.get("proposition_id")))
            item["relevance"]["recency_source"] = (
                "occurrence_time" if item.get("occurrence_time") is not None
                else "first_acquired_at" if item.get("first_acquired_at") is not None
                else "updated_at"
            )
            item["relevance"]["topology"] = round(topology_bonus, 6)
            item["relevance"]["total"] = round(
                float(item["relevance"]["total"]) + topology_bonus, 6
            )
            result.append(item)

        conflict_started = time.perf_counter()
        if result:
            proposition_ids = [item["proposition_id"] for item in result]
            conflict_rows = await self.db.fetch(
                """
                SELECT
                    base.proposition_id,
                    other.proposition_id AS competing_proposition_id,
                    other.canonical_text AS text,
                    pc.conflict_type,
                    pc.strength
                FROM unnest($1::uuid[]) base(proposition_id)
                JOIN aios.proposition_conflict pc
                  ON pc.proposition_a_id=base.proposition_id
                  OR pc.proposition_b_id=base.proposition_id
                JOIN aios.proposition other
                  ON other.proposition_id = CASE
                      WHEN pc.proposition_a_id=base.proposition_id
                      THEN pc.proposition_b_id
                      ELSE pc.proposition_a_id
                  END
                WHERE EXISTS (
                    SELECT 1
                    FROM aios.character_proposition_knowledge other_ck
                    WHERE other_ck.instance_id = ANY($2::uuid[])
                      AND other_ck.proposition_id=other.proposition_id
                )
                ORDER BY pc.strength DESC
                """,
                proposition_ids,
                lineage_ids,
            )
            by_proposition: dict[Any, list[dict[str, Any]]] = {}
            for conflict in conflict_rows:
                entry = dict(conflict)
                base_id = entry.pop("proposition_id")
                by_proposition.setdefault(base_id, []).append(entry)
            for item in result:
                item["conflicts"] = by_proposition.get(item["proposition_id"], [])
        conflict_ms = (time.perf_counter() - conflict_started) * 1000.0

        event_by_proposition = await self._canonical_event_memberships(
            [item["proposition_id"] for item in result]
        )
        result = self._collapse_canonical_events(result, event_by_proposition)
        episode_by_event = await self._episode_memberships(
            [item["semantic_event_id"] for item in result if item.get("semantic_event_id")]
        )
        result = self._collapse_semantic_episodes(result, episode_by_event)
        result.sort(
            key=lambda item: (
                -item["relevance"]["total"],
                item["topology"]["historical"],
                item["topology"]["cost"],
            )
        )
        total_ms = (time.perf_counter() - started) * 1000.0
        log = logger.info if total_ms >= 250.0 else logger.debug
        log(
            "HUD topology retrieval mode=%s rows=%d fallback=%s semantic_seed_ms=%.1f "
            "topology_sql_ms=%.1f anchor_ms=%.1f conflict_ms=%.1f total_ms=%.1f",
            mode,
            len(result),
            topology_fallback,
            semantic_ms,
            topology_ms,
            anchor_ms,
            conflict_ms,
            total_ms,
        )
        return result
