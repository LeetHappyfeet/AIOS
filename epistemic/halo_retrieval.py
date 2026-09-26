from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
from typing import Any, Iterable, Optional

from aios_app.hud.context import HUDContext
from aios_app.hud.relevance import HUDRelevanceScorer
from aios_app.epistemic.retrieval import (
    POLICIES,
    SEMANTIC_SEED_WAIT_SECONDS,
    RetrievalPolicy,
    TopologyRetriever as BaseTopologyRetriever,
)
from aios_app.epistemic.world_projection import build_character_world_query
from aios_app.epistemic.world_retrieval import WorldPropositionRetriever
from aios_app.epistemic.world_scope import build_retrieval_scope, normalize_domain

logger = logging.getLogger("aios.epistemic.halo")
HALO_PREDECESSOR_NODES = 4
HALO_MAX_TEXT_CHARS = 2400
SEMANTIC_SCOPE_MIN_HITS = 8
_DEFAULT_WORLD_DOMAIN_BY_MODE = {"memory": "history", "event": "history", "belief": "general", "goal": "general", "rule": "general"}
_ACTIVE_WORLD_DOMAIN: ContextVar[str] = ContextVar("aios_active_world_retrieval_domain", default="general")


class TopologyRetriever(BaseTopologyRetriever):
    """Federated /char + public /world retrieval with a bounded DAG halo."""

    def __init__(self, db: Any):
        super().__init__(db)
        self._halo_cache: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
        self._world_cycle_cache: dict[str, list[dict[str, Any]]] = {}
        self.world = WorldPropositionRetriever(db)

    def begin_retrieval_cycle(self) -> None:
        super().begin_retrieval_cycle()
        self._world_cycle_cache.clear()

    async def _dag_halo(self, context: HUDContext) -> tuple[str, tuple[str, ...]]:
        timeline_id = context.source_timeline_id or context.timeline_id
        head_node_id = context.source_head_node_id or context.head_node_id
        if not timeline_id or not head_node_id:
            return "", ()
        cache_key = (str(timeline_id), str(head_node_id))
        cached = self._halo_cache.get(cache_key)
        if cached is not None:
            return cached
        if context.source_timeline_id and context.source_head_node_id:
            rows = await self.db.fetch(
                """SELECT dn.node_id, dn.event_id, dn.message_text
                FROM aios.dag_node dn JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
                JOIN aios.dag_node head ON head.node_id=$2 AND head.timeline_id=$1
                WHERE dn.timeline_id=$1 AND dn.event_id < head.event_id
                  AND dn.message_text IS NOT NULL AND btrim(dn.message_text) <> ''
                  AND ie.superseded_at IS NULL ORDER BY dn.event_id DESC LIMIT $3""",
                timeline_id, head_node_id, HALO_PREDECESSOR_NODES,
            )
        else:
            rows = await self.db.fetch(
                """SELECT dn.node_id, dn.event_id, dn.message_text
                FROM aios.dag_node dn JOIN aios.dag_node head ON head.node_id=$2 AND head.timeline_id=$1
                WHERE dn.timeline_id=$1 AND dn.event_id < head.event_id
                  AND dn.message_text IS NOT NULL AND btrim(dn.message_text) <> ''
                ORDER BY dn.event_id DESC LIMIT $3""",
                timeline_id, head_node_id, HALO_PREDECESSOR_NODES,
            )
        node_ids = tuple(str(row["node_id"]) for row in rows)
        halo_text = " ".join(str(row["message_text"]).strip() for row in reversed(rows) if row.get("message_text"))
        if len(halo_text) > HALO_MAX_TEXT_CHARS:
            halo_text = halo_text[-HALO_MAX_TEXT_CHARS:]
        result = (halo_text, node_ids)
        self._halo_cache[cache_key] = result
        if len(self._halo_cache) > 128:
            self._halo_cache.pop(next(iter(self._halo_cache)))
        return result

    async def _query_semantic_seed_propositions(self, context: HUDContext, *, query_text: str, cache_key: tuple[Any, ...]) -> dict[str, float]:
        try:
            domain = _ACTIVE_WORLD_DOMAIN.get()
            scope = await build_retrieval_scope(self.db, world_id=context.world_id, domain=domain)
            hits = await asyncio.to_thread(
                self.semantic.search_epistemic_staged, query_text,
                character_id=context.character_id, instance_ids=context.lineage_instance_ids,
                world_stages=scope.qdrant_world_stages, min_hits=SEMANTIC_SCOPE_MIN_HITS,
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
            logger.debug("Scoped semantic seed lookup unavailable; using topology/lexical fallback: %s", exc)
            self._semantic_seed_cache[cache_key] = {}
            return {}
        finally:
            self._semantic_seed_deferred.discard(cache_key)

    async def _semantic_seed_propositions(self, context: HUDContext, *, focus_text: str, goals: Iterable[Any]) -> dict[str, float]:
        query_text = " ".join(part for part in (focus_text, " ".join(str(goal) for goal in goals)) if part).strip()
        if not query_text:
            return {}
        domain = _ACTIVE_WORLD_DOMAIN.get()
        lineage = tuple(str(value) for value in context.lineage_instance_ids)
        cache_key = (str(context.character_id), str(context.world_id), domain, query_text, lineage)
        cached = self._semantic_seed_cache.get(cache_key)
        if cached is not None:
            return cached
        if cache_key in self._semantic_seed_deferred:
            return {}
        try:
            return await asyncio.wait_for(
                self._semantic_seed_flights.run(cache_key, lambda: self._query_semantic_seed_propositions(context, query_text=query_text, cache_key=cache_key)),
                timeout=SEMANTIC_SEED_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._semantic_seed_deferred.add(cache_key)
            logger.debug("HUD scoped semantic seed exceeded %.0f ms budget; using lexical/topology fallback", SEMANTIC_SEED_WAIT_SECONDS * 1000.0)
            return {}

    async def retrieve_character_knowledge(
        self, context: HUDContext, scorer: HUDRelevanceScorer, *, mode: str,
        focus_text: str = "", goals: Iterable[Any] = (), max_hops: Optional[int] = None,
        limit: Optional[int] = None, world_domain: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        halo_text, halo_node_ids = await self._dag_halo(context)
        expanded_focus = " ".join(part for part in (halo_text, focus_text) if part).strip()
        domain = normalize_domain(world_domain or _DEFAULT_WORLD_DOMAIN_BY_MODE.get(mode, "general"))
        policy = POLICIES.get(mode)
        effective_limit = int(limit or (policy.limit if policy else 30))
        token = _ACTIVE_WORLD_DOMAIN.set(domain)
        try:
            char_result = await super().retrieve_character_knowledge(
                context, scorer, mode=mode, focus_text=expanded_focus, goals=goals,
                max_hops=max_hops, limit=limit,
            )
        finally:
            _ACTIVE_WORLD_DOMAIN.reset(token)

        # If character topology missed its generation budget, do not compound
        # the miss with optional public-world I/O. CognitiveContext already has
        # the bounded direct-character path running concurrently; return control
        # to it immediately. A later prepared/cache cycle can enrich with world
        # knowledge when the accelerators recover.
        if self.topology_degraded and not char_result:
            logger.info(
                "Federated halo mode=%s domain=%s skipped world after topology budget miss",
                mode,
                domain,
            )
            return []

        world_result: list[dict[str, Any]] = []
        try:
            # World augmentation is domain-scoped, not mode-scoped. Fetch a
            # superset once for history/general during this prepared HUD cycle,
            # then partition it by claim kind locally. This avoids repeating
            # Qdrant + SQL fallback for memory/event and belief/goal/rule.
            domain_rows = self._world_cycle_cache.get(domain)
            if domain_rows is None:
                projection = await build_character_world_query(
                    self.db, context, focus_text=focus_text, halo_text=halo_text, goals=goals,
                )
                domain_rows = await self.world.retrieve(
                    context, scorer, query_text=projection.query_text, domain=domain,
                    claim_kinds=(), limit=max(60, effective_limit),
                )
                self._world_cycle_cache[domain] = domain_rows
            allowed_kinds = set(policy.claim_kinds if policy else ())
            world_result = [
                item for item in domain_rows
                if not allowed_kinds
                or str(item.get("claim_kind") or "BELIEF").upper() in allowed_kinds
            ][:effective_limit]
        except Exception as exc:
            logger.warning(
                "Public world retrieval failed mode=%s domain=%s; preserving /char result",
                mode, domain, exc_info=True,
            )

        merged: list[dict[str, Any]] = []
        seen_propositions: set[str] = set()
        for item in char_result:
            proposition_id = str(item.get("proposition_id") or "")
            if proposition_id:
                seen_propositions.add(proposition_id)
            item.setdefault("retrieval_scope", "character")
            item.setdefault("retrieval_reason", "owned")
            merged.append(item)
        for item in world_result:
            proposition_id = str(item.get("proposition_id") or "")
            if proposition_id and proposition_id in seen_propositions:
                continue
            if proposition_id:
                seen_propositions.add(proposition_id)
            merged.append(item)

        def _score(item: dict[str, Any]) -> float:
            relevance = item.get("relevance") or {}
            try:
                return float(relevance.get("total") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        merged.sort(key=lambda item: (-_score(item), 0 if item.get("retrieval_scope") == "character" else 1))
        result = merged[:effective_limit]
        logger.info(
            "Federated halo mode=%s domain=%s nodes=%s char=%d world=%d merged=%d",
            mode, domain, halo_node_ids, len(char_result), len(world_result), len(result),
        )
        return result


__all__ = ["HALO_PREDECESSOR_NODES", "HALO_MAX_TEXT_CHARS", "SEMANTIC_SCOPE_MIN_HITS", "POLICIES", "RetrievalPolicy", "TopologyRetriever"]
