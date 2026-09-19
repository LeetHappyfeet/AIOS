from __future__ import annotations

import asyncio
from typing import Any, Iterable

from aios_app.epistemic.world_scope import build_retrieval_scope
from aios_app.hud.context import HUDContext
from aios_app.hud.relevance import HUDRelevanceScorer
from aios_app.semantic_index.query import SemanticQueryService


class WorldPropositionRetriever:
    """Retrieve public world propositions without manufacturing character ownership."""

    def __init__(self, db: Any):
        self.db = db
        self.semantic = SemanticQueryService()

    async def retrieve(
        self,
        context: HUDContext,
        scorer: HUDRelevanceScorer,
        *,
        query_text: str,
        domain: str = "general",
        claim_kinds: Iterable[str] = (),
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        if not query_text.strip() or context.world_id is None:
            return []

        scope = await build_retrieval_scope(
            self.db,
            world_id=context.world_id,
            domain=domain,
        )
        hits = await asyncio.to_thread(
            self.semantic.search_epistemic_staged,
            query_text,
            character_id=context.character_id,
            instance_ids=context.lineage_instance_ids,
            world_stages=scope.qdrant_world_stages,
            min_hits=max(8, min(limit, 24)),
        )

        ids: list[str] = []
        seen: set[str] = set()
        for _, _, payload in hits:
            pid = str(payload.get("proposition_id") or "")
            if pid and pid not in seen:
                seen.add(pid)
                ids.append(pid)
            if len(ids) >= max(limit * 3, 48):
                break
        if not ids:
            return []

        rows = await self.db.fetch(
            """
            WITH eligible_observations AS (
                SELECT
                    p.proposition_id, p.topic_key, p.canonical_text,
                    p.subject_norm, p.predicate_norm, p.object_norm,
                    p.polarity, p.modality,
                    obs.claim_id AS evidence_claim_id,
                    ccr.claim_kind, ccr.predicate_family,
                    ccr.world_id AS source_world_id,
                    ccr.dag_node_id AS source_node_id,
                    ccr.resolved_at,
                    dn.event_time AS occurrence_time
                FROM aios.proposition p
                JOIN aios.observation obs ON obs.proposition_id=p.proposition_id
                JOIN aios.claim_context_resolution ccr ON ccr.claim_id=obs.claim_id
                LEFT JOIN aios.claim_candidate cc ON cc.claim_id=obs.claim_id
                LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
                LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
                LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id
                LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
                WHERE p.proposition_id = ANY($1::uuid[])
                  AND ccr.world_id = ANY($2::uuid[])
                  AND ccr.character_instance_id IS NULL
                  AND (ie.event_id IS NULL OR ie.superseded_at IS NULL)
                  AND (cardinality($3::text[]) = 0 OR ccr.claim_kind = ANY($3::text[]))
            )
            SELECT DISTINCT ON (proposition_id)
                proposition_id, topic_key, canonical_text,
                subject_norm, predicate_norm, object_norm,
                polarity, modality,
                evidence_claim_id,
                claim_kind, predicate_family,
                source_world_id, source_node_id,
                occurrence_time
            FROM eligible_observations
            ORDER BY
                proposition_id,
                -- Keep the selected world observation internally coherent:
                -- claim kind, predicate family, world and node all come from
                -- the same claim occurrence. Prefer the current world over an
                -- ancestor when the proposition exists in both.
                (source_world_id = $4::uuid) DESC,
                resolved_at DESC,
                evidence_claim_id DESC
            """,
            ids,
            list(scope.all_world_ids),
            list(claim_kinds),
            context.world_id,
        )

        rank_by_id = {pid: rank for rank, pid in enumerate(ids)}
        current_world = str(context.world_id)
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["text"] = item.pop("canonical_text")
            # World cognition is occurrence-scoped even when proposition text
            # is shared by several claims. Preserve the claim and retrieval
            # route so downstream cognition never has to infer kind from the
            # proposition alone.
            item["retrieval_mode"] = domain
            item["world_evidence_claim_id"] = item.get("evidence_claim_id")
            rank = rank_by_id.get(str(item["proposition_id"]), len(ids))
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=(
                    f"{item.get('topic_key','')} {item.get('subject_norm','')} "
                    f"{item.get('predicate_norm','')} {item.get('object_norm','')} "
                    f"{item.get('text','')}"
                ),
                candidate_world_id=item.get("source_world_id") or context.world_id,
                candidate_entity_id=None,
                epistemic_status="PUBLIC_WORLD",
                confidence=None,
                updated_at=None,
                causal_distance=None,
            )
            source_world = str(item.get("source_world_id") or "")
            item["retrieval_scope"] = "world"
            item["retrieval_reason"] = (
                "world_current_semantic"
                if source_world == current_world
                else "world_lineage_semantic"
            )
            item["epistemic_status"] = "PUBLIC_WORLD"
            item["relevance"] = score.as_dict()
            result.append(item)

        result.sort(key=lambda item: -float(item["relevance"]["total"]))
        return result[:limit]
