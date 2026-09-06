from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from aios_app.db import Database
from aios_app.hud.context import HUDContext
from aios_app.hud.relevance import HUDRelevanceScorer


_WORD_RE = re.compile(r"[a-z0-9_'-]+")


@dataclass(frozen=True)
class RetrievalPolicy:
    name: str
    claim_kinds: tuple[str, ...]
    max_hops: int
    limit: int
    retain_topic_history: bool = False


POLICIES = {
    "memory": RetrievalPolicy(
        "memory",
        ("MEMORY", "EVENT"),
        max_hops=3,
        limit=60,
        retain_topic_history=True,
    ),
    "belief": RetrievalPolicy(
        "belief",
        ("BELIEF", "TRAIT", "STATE", "CONCEPT"),
        max_hops=2,
        limit=60,
        retain_topic_history=False,
    ),
    "goal": RetrievalPolicy(
        "goal",
        ("GOAL",),
        max_hops=2,
        limit=30,
        retain_topic_history=False,
    ),
    "event": RetrievalPolicy(
        "event",
        ("EVENT",),
        max_hops=2,
        limit=40,
        retain_topic_history=True,
    ),
    "rule": RetrievalPolicy(
        "rule",
        ("RULE",),
        max_hops=1,
        limit=30,
        retain_topic_history=False,
    ),
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
            if len(result) >= 24:
                return result
    return result


class TopologyRetriever:
    """
    Branch-safe retrieval over derived semantic topology.

    Hard eligibility happens in SQL before ranking:
      * /char scope must match the active character.
      * only the active character instance and its ancestors are visible.
      * sibling experiential branches never enter the candidate set.

    Topology is navigation, not truth. Character knowledge remains authoritative
    for what the active character actually owns; topology only decides where to
    look and how costly that path is.
    """

    def __init__(self, db: Database):
        self.db = db

    async def retrieve_character_knowledge(
        self,
        context: HUDContext,
        scorer: HUDRelevanceScorer,
        *,
        mode: str,
        focus_text: str = "",
        goals: Iterable[Any] = (),
        max_hops: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        policy = POLICIES.get(mode)
        if policy is None:
            raise ValueError(f"unknown retrieval mode '{mode}'")

        hops = max(0, min(int(max_hops if max_hops is not None else policy.max_hops), 6))
        row_limit = max(1, min(int(limit if limit is not None else policy.limit), 250))
        scope_key = f"char:{context.character_id}"
        lineage_ids = list(context.lineage_instance_ids)
        lineage_keys = [str(value) for value in context.lineage_instance_ids]
        terms = _focus_terms(focus_text, " ".join(str(goal) for goal in goals))

        rows = await self.db.fetch(
            """
            WITH RECURSIVE
            eligible_nodes AS (
                SELECT n.*
                FROM aios.semantic_topology_node n
                WHERE n.scope_key=$1
                  AND (
                      n.character_instance_id IS NULL
                      OR n.character_instance_id = ANY($2::uuid[])
                      OR (
                          n.node_type='INSTANCE'
                          AND n.node_key = ANY($3::text[])
                      )
                  )
            ),
            seeds AS (
                SELECT topology_node_id
                FROM eligible_nodes
                WHERE (
                    node_type='INSTANCE'
                    AND node_key=$4
                )
                OR (
                    cardinality($5::text[]) > 0
                    AND EXISTS (
                        SELECT 1
                        FROM unnest($5::text[]) term
                        WHERE lower(COALESCE(label,'')) LIKE '%' || term || '%'
                           OR lower(node_key) LIKE '%' || term || '%'
                    )
                )
            ),
            walk(topology_node_id, depth, path_cost, path) AS (
                SELECT s.topology_node_id, 0, 0.0::double precision,
                       ARRAY[s.topology_node_id]::uuid[]
                FROM seeds s

                UNION ALL

                SELECT
                    CASE
                        WHEN e.parent_node_id=w.topology_node_id THEN e.child_node_id
                        ELSE e.parent_node_id
                    END,
                    w.depth + 1,
                    w.path_cost + CASE e.edge_type
                        WHEN 'experiential_branch' THEN 0.0
                        WHEN 'forks_at' THEN 0.1
                        WHEN 'contains_branch' THEN 0.35
                        WHEN 'epistemic_transition' THEN 0.25
                        WHEN 'about_topic' THEN 0.7
                        WHEN 'subject_pivot' THEN 0.65
                        WHEN 'object_pivot' THEN 0.65
                        WHEN 'acquires' THEN 0.25
                        WHEN 'acquired_from' THEN 1.1
                        WHEN 'contains_assertion' THEN 0.3
                        WHEN 'asserts_topic' THEN 0.4
                        ELSE 0.8
                    END,
                    w.path || CASE
                        WHEN e.parent_node_id=w.topology_node_id THEN e.child_node_id
                        ELSE e.parent_node_id
                    END
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
                WHERE w.depth < $6
                  AND NOT (
                      CASE
                          WHEN e.parent_node_id=w.topology_node_id THEN e.child_node_id
                          ELSE e.parent_node_id
                      END = ANY(w.path)
                  )
            ),
            nearest AS (
                SELECT topology_node_id, MIN(depth) AS topology_depth,
                       MIN(path_cost) AS topology_cost
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
            owned AS (
                SELECT DISTINCT ON (ck.proposition_id)
                    ck.instance_id,
                    ck.proposition_id,
                    ck.epistemic_status,
                    ck.confidence,
                    ck.acquisition_mode,
                    ck.source_entity_id,
                    ck.first_node_id,
                    ck.last_node_id,
                    ck.updated_at,
                    ck.base_confidence,
                    ck.attention_weight,
                    ck.trust_weight,
                    ck.compatibility_weight,
                    ck.retention_weight,
                    ck.salience_weight,
                    ck.effective_confidence,
                    array_position($2::uuid[], ck.instance_id) AS instance_depth
                FROM aios.character_proposition_knowledge ck
                WHERE ck.instance_id = ANY($2::uuid[])
                ORDER BY ck.proposition_id,
                         array_position($2::uuid[], ck.instance_id),
                         ck.updated_at DESC
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
                    tp.topology_depth,
                    tp.topology_cost,
                    tp.topology_significance
                FROM topology_props tp
                JOIN owned o ON o.proposition_id=tp.proposition_id
                JOIN aios.proposition p ON p.proposition_id=o.proposition_id
                LEFT JOIN LATERAL (
                    SELECT ccr.claim_kind, ccr.predicate_family,
                           ccr.world_id, ccr.dag_node_id
                    FROM aios.observation obs
                    JOIN aios.claim_context_resolution ccr ON ccr.claim_id=obs.claim_id
                    WHERE obs.proposition_id=p.proposition_id
                      AND (
                          ccr.character_instance_id IS NULL
                          OR ccr.character_instance_id = ANY($2::uuid[])
                      )
                    ORDER BY
                        array_position($2::uuid[], ccr.character_instance_id) NULLS LAST,
                        ccr.resolved_at DESC
                    LIMIT 1
                ) ctx ON true
                WHERE COALESCE(ctx.claim_kind, 'BELIEF') = ANY($7::text[])
            ),
            topic_ranked AS (
                SELECT c.*,
                       row_number() OVER (
                           PARTITION BY c.topic_key
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
            WHERE $8::boolean OR topic_recency_rank=1
            ORDER BY
                topology_cost,
                topology_depth,
                topology_significance DESC,
                instance_depth,
                updated_at DESC
            LIMIT $9
            """,
            scope_key,
            lineage_ids,
            lineage_keys,
            str(context.instance_id),
            terms,
            hops,
            list(policy.claim_kinds),
            bool(policy.retain_topic_history),
            row_limit,
        )

        result: list[dict[str, Any]] = []
        for rank, row in enumerate(rows):
            item = dict(row)
            item["text"] = item.pop("canonical_text")
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=(
                    f"{item.get('topic_key','')} {item.get('subject_norm','')} "
                    f"{item.get('predicate_norm','')} {item.get('object_norm','')} "
                    f"{item.get('text','')}"
                ),
                candidate_world_id=context.world_id,
                candidate_entity_id=item.get("source_entity_id"),
                epistemic_status=item.get("epistemic_status"),
                confidence=item.get("effective_confidence") or item.get("confidence"),
                updated_at=item.get("updated_at"),
                causal_distance=item.get("topology_depth"),
            )
            topology_bonus = (
                1.5 / (1.0 + float(item.get("topology_cost") or 0.0))
                + 0.7 * float(item.get("topology_significance") or 0.0)
            )
            item["topology"] = {
                "depth": int(item.get("topology_depth") or 0),
                "cost": float(item.get("topology_cost") or 0.0),
                "significance": float(item.get("topology_significance") or 0.0),
                "topic_recency_rank": int(item.get("topic_recency_rank") or 1),
                "historical": int(item.get("topic_recency_rank") or 1) > 1,
                "instance_depth": int(item.get("instance_depth") or 0),
            }
            item["relevance"] = score.as_dict()
            item["relevance"]["topology"] = round(topology_bonus, 6)
            item["relevance"]["total"] = round(
                float(item["relevance"]["total"]) + topology_bonus,
                6,
            )
            result.append(item)

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

        result.sort(
            key=lambda item: (
                item["topology"]["historical"],
                -item["relevance"]["total"],
                item["topology"]["cost"],
            )
        )
        return result
