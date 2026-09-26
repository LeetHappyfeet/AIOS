from __future__ import annotations

import copy
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService, CognitiveGoal
from aios_app.hud.context import HUDContext
from aios_app.hud.retrieval import TopologyRetriever
from aios_app.hud.singleflight import AsyncSingleFlight
from aios_app.epistemic.message_cognition import current_message_cognition
from aios_app.epistemic.relevance import CognitiveRelevanceScorer, select_recalled_cognition
from aios_app.epistemic.research import (
    CharacterResearchService,
    CorpusLearningService,
    CorpusReinforcementService,
    SemanticCorpusReinforcementService,
    SemanticKnowledgeCoverageService,
    KnowledgeDemandResolver,
)
from aios_app.epistemic.retrieval_policy import (
    CognitiveRetrievalPolicy,
    DEFAULT_COGNITIVE_RETRIEVAL_POLICY,
)


logger = logging.getLogger("aios.epistemic.cognitive_context")
PREPARED_RETRIEVAL_TTL_SECONDS = 15.0
PREPARED_RETRIEVAL_CACHE_SIZE = 32


class RelevanceScorer(Protocol):
    def score(self, item: Mapping[str, Any], **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class CognitiveAttentionInputs:
    recent_newest: list[dict[str, Any]]
    visible_source_node_ids: frozenset[str]
    focus_text: str
    plugin_focus_text: str
    retrieval_focus_text: str
    goals: list[CognitiveGoal]


@dataclass(frozen=True)
class CognitiveKnowledgeSnapshot:
    knowledge: list[dict[str, Any]]
    recalled_memories: list[dict[str, Any]]
    beliefs: list[dict[str, Any]]
    goals: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    current_events: list[dict[str, Any]]
    corpus_references: list[dict[str, Any]]
    corpus_demand: dict[str, Any] | None
    recall_suppressed: dict[str, int]
    topology_retrieval: bool
    topology_partial_fallback: bool
    firewall_suppressed: dict[str, int]
    anchored_knowledge_count: int
    visible_world_context_count: int
    invisible_anchor_count: int


@dataclass(frozen=True)
class PreparedRetrievalSnapshot:
    topology_knowledge: list[dict[str, Any]]
    legacy_knowledge: list[dict[str, Any]]
    missing_modes: dict[str, bool]
    prepared_at: float


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def cognitive_rejection_reason(
    item: Mapping[str, Any],
    *,
    visible_source_node_ids: frozenset[str] = frozenset(),
) -> str | None:
    """Reject immature archaeology hypotheses before HUD presentation.

    Message-level cognitive commits are already bounded/admitted by the fast
    interpreter and intentionally bypass the current-source duplicate guard.
    They represent cognition derived from the source event, not a second copy of
    the raw event.
    """
    if item.get("cognitive_commit"):
        return None

    source_node_id = item.get("source_node_id")
    if source_node_id is not None and str(source_node_id) in visible_source_node_ids:
        return "current_source_duplicate"

    text = str(item.get("text") or "").strip()
    if not text:
        return "empty_text"
    if "frame:" in text.lower():
        return "unresolved_frame"

    subject = str(item.get("subject_norm") or "").strip().lower()
    predicate = str(item.get("predicate_norm") or "").strip().lower()
    if subject in {"", "_", "*"}:
        return "missing_subject"
    if predicate in {"", "_", "*"}:
        return "missing_predicate"

    kind = str(item.get("claim_kind") or "BELIEF").upper()
    object_value = str(item.get("object_norm") or "").strip().lower()
    if kind in {"BELIEF", "TRAIT", "STATE", "CONCEPT", "GOAL", "RULE", "RELATIONSHIP"}:
        if object_value in {"", "_", "*"}:
            return "missing_object"

    confidence = item.get("effective_confidence")
    if not isinstance(confidence, (float, int)):
        confidence = item.get("confidence")
    if isinstance(confidence, (float, int)) and float(confidence) <= 0.0:
        return "nonpositive_confidence"

    return None


def automatic_corpus_research_allowed(
    attention: CognitiveAttentionInputs,
    *,
    character_id: str,
) -> bool:
    """Allow automatic lookup only when the current focus came from outside the character."""
    focus_row = next(
        (row for row in attention.recent_newest if row.get("message_text")),
        None,
    )
    if not focus_row:
        return False
    speaker_id = str(focus_row.get("speaker_id") or "").strip()
    speaker_role = str(focus_row.get("speaker_role") or "").strip().lower()
    if speaker_id and speaker_id == str(character_id):
        return False
    if speaker_role == "assistant":
        return False
    return True


def admit_cognitive_candidates(
    items: Iterable[dict[str, Any]],
    *,
    visible_source_node_ids: frozenset[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    admitted: list[dict[str, Any]] = []
    suppressed: dict[str, int] = {}
    for item in items:
        reason = cognitive_rejection_reason(
            item,
            visible_source_node_ids=visible_source_node_ids,
        )
        if reason is None:
            admitted.append(item)
            continue
        suppressed[reason] = suppressed.get(reason, 0) + 1
    return admitted, suppressed


class CognitiveContextService:
    """Resolve source visibility and character cognition before HUD presentation."""

    def __init__(self, db: Database, *, retrieval_policy: CognitiveRetrievalPolicy | None = None):
        self.db = db
        self.retriever = TopologyRetriever(db)
        self.goals = CharacterGoalService(db)
        self.research = CharacterResearchService(db)
        self.corpus_learning = CorpusLearningService(db)
        self.corpus_reinforcement = CorpusReinforcementService(db)
        self.semantic_corpus_reinforcement = SemanticCorpusReinforcementService(db)
        self.semantic_knowledge_coverage = SemanticKnowledgeCoverageService()
        self.knowledge_demand = KnowledgeDemandResolver(
            minimum_terms=2,
            coverage_threshold=0.60,
        )
        self.retrieval_policy = retrieval_policy or DEFAULT_COGNITIVE_RETRIEVAL_POLICY
        self._prepared_retrieval: dict[tuple[Any, ...], PreparedRetrievalSnapshot] = {}
        self._prepared_retrieval_flights: AsyncSingleFlight[
            tuple[Any, ...], PreparedRetrievalSnapshot
        ] = AsyncSingleFlight()

    async def resolve_attention_inputs(
        self,
        context: HUDContext,
        raw_state: Mapping[str, Any],
        plugin_snapshot: Mapping[str, Any],
        *,
        recent_limit: int,
        focus_text: str | None = None,
    ) -> CognitiveAttentionInputs:
        bounded_limit = max(1, min(int(recent_limit), 100))

        runtime_rows = await self.db.fetch(
            """
            SELECT node_id, event_id, event_time, speaker_id, speaker_role,
                   message_text, payload
            FROM aios.dag_node
            WHERE timeline_id=$1
            ORDER BY event_id DESC
            LIMIT $2
            """,
            context.timeline_id,
            bounded_limit,
        )
        runtime_newest = [{**dict(row), "event_stream": "runtime"} for row in runtime_rows]

        source_newest: list[dict[str, Any]] = []
        if context.source_timeline_id and context.source_head_node_id:
            source_rows = await self.db.fetch(
                """
                SELECT dn.node_id, dn.event_id, dn.event_time, dn.speaker_id,
                       dn.speaker_role, dn.message_text, dn.payload
                FROM aios.dag_node dn
                JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
                JOIN aios.dag_node source_head
                  ON source_head.node_id=$2
                 AND source_head.timeline_id=$1
                WHERE dn.timeline_id=$1
                  AND ie.superseded_at IS NULL
                  AND dn.event_id <= source_head.event_id
                ORDER BY dn.event_id DESC
                LIMIT $3
                """,
                context.source_timeline_id,
                context.source_head_node_id,
                bounded_limit,
            )
            source_newest = [{**dict(row), "event_stream": "source"} for row in source_rows]

        recent_newest = source_newest + runtime_newest
        recent_newest.sort(
            key=lambda row: (
                row.get("event_time") is not None,
                row.get("event_time"),
                row.get("event_id", -1),
            ),
            reverse=True,
        )
        recent_newest = recent_newest[:bounded_limit]

        visible_source_node_ids = frozenset(
            str(row["node_id"])
            for row in recent_newest
            if row.get("node_id") is not None and row.get("message_text")
        )
        event_focus_text = next(
            (row.get("message_text") for row in recent_newest if row.get("message_text")),
            "",
        )
        resolved_focus_text = (
            str(focus_text).strip() if focus_text is not None and str(focus_text).strip()
            else event_focus_text
        )
        goal_set = await self.goals.resolve_active(
            context.instance_id,
            legacy_goals=raw_state.get("goals"),
        )
        goals = list(goal_set.active)
        plugin_focus_text = " ".join(
            str(signal.get("focus_text") or "")
            for signal in sorted(
                plugin_snapshot.get("retrieval_signals") or [],
                key=lambda item: float(item.get("strength") or 0.0),
                reverse=True,
            )
            if signal.get("focus_text")
        )
        retrieval_focus_text = " ".join(
            part for part in (resolved_focus_text, plugin_focus_text) if part
        )
        return CognitiveAttentionInputs(
            recent_newest=recent_newest,
            visible_source_node_ids=visible_source_node_ids,
            focus_text=resolved_focus_text,
            plugin_focus_text=plugin_focus_text,
            retrieval_focus_text=retrieval_focus_text,
            goals=goals,
        )

    def _prepared_retrieval_key(
        self,
        context: HUDContext,
        attention: CognitiveAttentionInputs,
        retrieval_policy: CognitiveRetrievalPolicy | None = None,
    ) -> tuple[Any, ...]:
        return (
            str(context.instance_id),
            str(context.character_id),
            str(context.source_head_node_id or ""),
            int(context.state_version or 0),
            tuple(str(value) for value in (context.cognitive_instance_ids or context.lineage_instance_ids)),
            attention.retrieval_focus_text,
            json.dumps(attention.goals, sort_keys=True, default=str),
            int((retrieval_policy or self.retrieval_policy).memory_hops),
            int((retrieval_policy or self.retrieval_policy).belief_hops),
            int((retrieval_policy or self.retrieval_policy).event_hops),
            int((retrieval_policy or self.retrieval_policy).goal_hops),
            int((retrieval_policy or self.retrieval_policy).rule_hops),
            int((retrieval_policy or self.retrieval_policy).semantic_retrieval_limit),
            int((retrieval_policy or self.retrieval_policy).deep_memory_limit),
        )

    async def prepare_retrieval(
        self,
        context: HUDContext,
        scorer: RelevanceScorer | None,
        attention: CognitiveAttentionInputs,
        retrieval_policy: CognitiveRetrievalPolicy | None = None,
    ) -> PreparedRetrievalSnapshot:
        """Speculatively prepare established-memory candidates for the next HUD.

        Current-message cognitive commits are intentionally excluded. They are
        always read fresh by ``resolve_knowledge`` so a prewarm that starts
        before fast cognition completes can never freeze an incomplete turn.
        """
        snapshot, cache_hit = await self._prepared_or_resolve(
            context,
            scorer,
            attention,
            retrieval_policy,
        )
        logger.debug(
            "Retrieval prewarm instance=%s node=%s cache_hit=%s candidates=%d legacy=%d",
            context.instance_id,
            context.source_head_node_id,
            cache_hit,
            len(snapshot.topology_knowledge),
            len(snapshot.legacy_knowledge),
        )
        return snapshot

    async def _prepared_or_resolve(
        self,
        context: HUDContext,
        scorer: RelevanceScorer | None,
        attention: CognitiveAttentionInputs,
        retrieval_policy: CognitiveRetrievalPolicy | None = None,
    ) -> tuple[PreparedRetrievalSnapshot, bool]:
        key = self._prepared_retrieval_key(context, attention, retrieval_policy)
        now = time.monotonic()
        cached = self._prepared_retrieval.get(key)
        if cached is not None:
            if now - cached.prepared_at <= PREPARED_RETRIEVAL_TTL_SECONDS:
                return cached, True
            self._prepared_retrieval.pop(key, None)

        snapshot = await self._prepared_retrieval_flights.run(
            key,
            lambda: self._resolve_retrieval_candidates(
                key,
                context,
                scorer,
                attention,
                retrieval_policy,
            ),
        )
        return snapshot, False

    async def _resolve_retrieval_candidates(
        self,
        key: tuple[Any, ...],
        context: HUDContext,
        scorer: RelevanceScorer | None,
        attention: CognitiveAttentionInputs,
        retrieval_policy: CognitiveRetrievalPolicy | None = None,
    ) -> PreparedRetrievalSnapshot:
        started = time.perf_counter()
        focus_text = attention.retrieval_focus_text
        goals = attention.goals
        policy = retrieval_policy or self.retrieval_policy
        scorer = scorer or CognitiveRelevanceScorer(
            context, focus_text=focus_text, goals=goals
        )

        # Start the bounded direct-character path at the same time as topology.
        # It is authoritative character knowledge, not merely an emergency query,
        # and gives generation a ready fail-open result if optional semantic
        # acceleration misses its latency budget.
        flat_task = asyncio.create_task(self._flat_character_knowledge(context, scorer))
        begin_cycle = getattr(self.retriever, "begin_retrieval_cycle", None)
        if begin_cycle is not None:
            begin_cycle()

        try:
            topology_memories = await self.retriever.retrieve_character_knowledge(
                context, scorer, mode="memory", focus_text=focus_text, goals=goals,
                max_hops=policy.effective_memory_hops,
                limit=policy.memory_limit,
            )
            topology_beliefs = await self.retriever.retrieve_character_knowledge(
                context, scorer, mode="belief", focus_text=focus_text, goals=goals,
                max_hops=policy.belief_hops,
                limit=policy.semantic_retrieval_limit,
            )
            topology_goals = await self.retriever.retrieve_character_knowledge(
                context, scorer, mode="goal", focus_text=focus_text, goals=goals,
                max_hops=policy.goal_hops,
                limit=min(policy.semantic_retrieval_limit, 30),
            )
            topology_events = await self.retriever.retrieve_character_knowledge(
                context, scorer, mode="event", focus_text=focus_text, goals=goals,
                max_hops=policy.event_hops,
                limit=min(policy.semantic_retrieval_limit, 40),
            )
            topology_rules = await self.retriever.retrieve_character_knowledge(
                context, scorer, mode="rule", focus_text=focus_text, goals=goals,
                max_hops=policy.rule_hops,
                limit=min(policy.semantic_retrieval_limit, 30),
            )
        except BaseException:
            if not flat_task.done():
                flat_task.cancel()
            try:
                await flat_task
            except (asyncio.CancelledError, Exception):
                pass
            raise

        topology_knowledge = (
            topology_memories + topology_beliefs + topology_goals + topology_events + topology_rules
        )
        missing_modes = {
            "memory": not topology_memories,
            "belief": not topology_beliefs,
            "goal": not topology_goals,
            "event": not topology_events,
            "rule": not topology_rules,
        }
        if any(missing_modes.values()):
            legacy_knowledge = await flat_task
        else:
            # Do not leave speculative work detached from the request. If the
            # topology path is complete, failure of an unused speculative
            # fallback must not fail cognition.
            if not flat_task.done():
                flat_task.cancel()
            try:
                await flat_task
            except (asyncio.CancelledError, Exception):
                pass
            legacy_knowledge = []
        snapshot = PreparedRetrievalSnapshot(
            topology_knowledge=topology_knowledge,
            legacy_knowledge=legacy_knowledge,
            missing_modes=missing_modes,
            prepared_at=time.monotonic(),
        )
        self._prepared_retrieval[key] = snapshot
        while len(self._prepared_retrieval) > PREPARED_RETRIEVAL_CACHE_SIZE:
            oldest_key = min(
                self._prepared_retrieval,
                key=lambda cache_key: self._prepared_retrieval[cache_key].prepared_at,
            )
            self._prepared_retrieval.pop(oldest_key, None)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log = logger.info if elapsed_ms >= 250.0 else logger.debug
        log(
            "Prepared retrieval instance=%s node=%s candidates=%d legacy=%d elapsed_ms=%.1f",
            context.instance_id,
            context.source_head_node_id,
            len(topology_knowledge),
            len(legacy_knowledge),
            elapsed_ms,
        )
        return snapshot

    async def resolve_knowledge(
        self,
        context: HUDContext,
        scorer: RelevanceScorer | None = None,
        attention: CognitiveAttentionInputs | None = None,
        retrieval_policy: CognitiveRetrievalPolicy | None = None,
    ) -> CognitiveKnowledgeSnapshot:
        scorer = scorer or CognitiveRelevanceScorer(
            context,
            focus_text=attention.retrieval_focus_text,
            goals=attention.goals,
        )

        # Fast cognition is generation-critical and deliberately independent of
        # topology/vector/RDF completion. It is merged fresh on every HUD build
        # even when the expensive established-memory retrieval was prewarmed.
        fast_rows = await current_message_cognition(
            self.db,
            instance_id=context.instance_id,
            node_id=context.source_head_node_id,
        )
        fast_knowledge: list[dict[str, Any]] = []
        for rank, row in enumerate(fast_rows):
            kind = str(row.get("claim_kind") or "BELIEF").upper()
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            confidence = float(row.get("confidence") or 0.5)
            meta = _json_value(row.get("meta"), {})
            semantic_owner = meta.get("semantic_owner")
            parsed_predicate = meta.get("predicate")
            parsed_object = meta.get("object")
            # Preserve the fast interpreter's semantic result. The bridge used
            # to overwrite every subject with the active character and every
            # predicate with "message_cognition", which destroyed exactly the
            # ownership/persistence evidence mature cognition later reasons on.
            fast_knowledge.append({
                "proposition_id": f"cognitive:{row['unit_id']}",
                "topic_key": row.get("topic_key"),
                "text": text,
                "subject_norm": semantic_owner or "",
                "predicate_norm": parsed_predicate or "",
                "object_norm": parsed_object or "",
                "polarity": row.get("polarity") or 1,
                "modality": "asserted",
                "claim_kind": kind,
                "predicate_family": "COGNITIVE_COMMIT",
                "epistemic_status": "observed",
                "confidence": confidence,
                "effective_confidence": confidence,
                "salience_weight": float(row.get("salience") or 0.5),
                "source_node_id": row.get("node_id"),
                "acquisition_mode": "message_cognitive_commit",
                "updated_at": None,
                "conflicts": [],
                "cognitive_commit": True,
                "cognitive_provisional": True,
                "cognitive_persistence": meta.get("persistence"),
                "character_owned": bool(meta.get("character_owned")),
                "parse_reason": meta.get("parse_reason"),
                "tier": 0 if kind in {"GOAL", "RULE"} else 1,
                # High enough to remain immediately available when enrichment
                # is late, but no longer an authority score. Recall selection
                # treats cognitive_commit as a provisional fallback channel.
                "relevance": {
                    "total": 2.0 + 0.45 * float(row.get("salience") or 0.5)
                             + 0.25 * confidence - rank * 0.01,
                    "source": "message_cognitive_commit",
                },
            })

        prepared, cache_hit = await self._prepared_or_resolve(
            context,
            scorer,
            attention,
            retrieval_policy,
        )
        # HUD shaping mutates candidate dictionaries (for example when hiding
        # provenance), so never hand the cache's objects directly to callers.
        topology_knowledge = copy.deepcopy(prepared.topology_knowledge)
        legacy_knowledge = copy.deepcopy(prepared.legacy_knowledge)
        missing_modes = dict(prepared.missing_modes)

        if cache_hit:
            logger.debug(
                "HUD retrieval cache hit instance=%s node=%s age_ms=%.1f",
                context.instance_id,
                context.source_head_node_id,
                (time.monotonic() - prepared.prepared_at) * 1000.0,
            )

        merged = list(fast_knowledge) + list(topology_knowledge)
        for item in legacy_knowledge:
            kind = str(item.get("claim_kind") or "BELIEF").upper()
            if (
                (kind == "MEMORY" and missing_modes["memory"])
                or (kind == "EVENT" and (missing_modes["memory"] or missing_modes["event"]))
                or (kind == "GOAL" and missing_modes["goal"])
                or (kind == "RULE" and missing_modes["rule"])
                or (
                    kind not in {"MEMORY", "RELATIONSHIP", "EVENT", "GOAL", "RULE"}
                    and missing_modes["belief"]
                )
            ):
                merged.append(item)

        seen: set[Any] = set()
        knowledge: list[dict[str, Any]] = []
        for item in merged:
            proposition_id = item.get("proposition_id")
            if proposition_id in seen:
                continue
            seen.add(proposition_id)
            knowledge.append(item)

        knowledge, suppressed = admit_cognitive_candidates(
            knowledge,
            visible_source_node_ids=attention.visible_source_node_ids,
        )
        knowledge, recall_suppressed = select_recalled_cognition(
            knowledge,
            focus_text=attention.retrieval_focus_text,
        )
        for reason, count in recall_suppressed.items():
            if count:
                suppressed[reason] = suppressed.get(reason, 0) + count

        recalled_memories: list[dict[str, Any]] = []
        beliefs: list[dict[str, Any]] = []
        goals: list[dict[str, Any]] = []
        rules: list[dict[str, Any]] = []
        for item in knowledge:
            kind = str(item.get("claim_kind") or "BELIEF").upper()
            if kind in {"MEMORY", "EVENT"}:
                recalled_memories.append(item)
            elif kind == "GOAL":
                goals.append(item)
            elif kind == "RULE":
                rules.append(item)
            else:
                beliefs.append(item)

        # Corpus research is a deterministic reference channel, not durable
        # character knowledge. Only established/recalled cognition is used to
        # judge coverage; corpus hits are never merged into knowledge/beliefs.
        # Prefer proposition structure over raw text when deciding whether
        # established /char cognition covers the current information need.
        # The lexical resolver remains as a conservative fallback for old or
        # provisional cognition that has not acquired semantic roles yet.
        structured_knowledge = [
            item for item in knowledge
            if item.get("subject_norm") or item.get("predicate_norm")
               or item.get("object_norm") or item.get("topic_key")
        ]
        if structured_knowledge:
            corpus_demand = self.semantic_knowledge_coverage.resolve(
                attention.retrieval_focus_text,
                knowledge=structured_knowledge,
                minimum_terms=2,
                threshold=0.60,
            )
        else:
            known_texts = [
                str(item.get("text") or "")
                for item in knowledge
                if item.get("text")
            ]
            corpus_demand = self.knowledge_demand.resolve(
                attention.retrieval_focus_text,
                known_texts=known_texts,
            )
        corpus_references: list[dict[str, Any]] = []
        corpus_result = None
        allow_automatic_corpus = automatic_corpus_research_allowed(
            attention,
            character_id=context.character_id,
        )
        if corpus_demand.needed and allow_automatic_corpus:
            # Search the missing concepts rather than replaying the entire turn.
            # This keeps dialogue/scaffolding words out of the FTS query.
            corpus_query = " OR ".join(corpus_demand.missing_terms)
            try:
                corpus_result = await self.research.search(
                    instance_id=context.instance_id,
                    query=corpus_query,
                    limit=5,
                )
                corpus_references = corpus_result.reference_context()
            except Exception:
                # Corpus lookup is supplementary. A missing migration, unavailable
                # corpus, or search failure must never block normal cognition.
                logger.exception(
                    "Corpus reference lookup failed instance=%s",
                    context.instance_id,
                )

        # Repeated external focus is stronger evidence of attention than a
        # one-turn lookup. Character-generated output must not recursively
        # reinforce or teach from corpus references either.
        if allow_automatic_corpus:
            try:
                current_research_id = (
                    corpus_result.research_id if corpus_result is not None else None
                )
                await self.corpus_reinforcement.reinforce_from_focus(
                    instance_id=context.instance_id,
                    focus_text=attention.retrieval_focus_text,
                    current_research_id=current_research_id,
                )
                await self.semantic_corpus_reinforcement.reinforce_from_knowledge(
                    instance_id=context.instance_id,
                    knowledge=structured_knowledge,
                    current_research_id=current_research_id,
                )
                if corpus_result is not None and corpus_result.hits:
                    await self.corpus_learning.evaluate_and_acquire(
                        instance_id=context.instance_id,
                        research_id=corpus_result.research_id,
                        section_ids=[hit.section_id for hit in corpus_result.hits],
                    )
            except Exception:
                # Learning is subordinate to cognition exactly like corpus search.
                # Failed acquisition must not make the HUD unavailable.
                logger.exception(
                    "Corpus learning evaluation failed instance=%s",
                    context.instance_id,
                )

        corpus_demand_meta = {
            "needed": corpus_demand.needed,
            "terms": list(corpus_demand.terms),
            "missing_terms": list(corpus_demand.missing_terms),
            "coverage": corpus_demand.coverage,
            "reason": corpus_demand.reason,
            "automatic_lookup_allowed": allow_automatic_corpus,
        }

        anchored_knowledge_count = sum(1 for item in knowledge if item.get("anchor"))
        visible_world_context_count = sum(
            len(item.get("world_context") or [])
            for item in knowledge
            if item.get("anchor", {}).get("world_visible")
        )
        invisible_anchor_count = sum(
            1 for item in knowledge
            if item.get("anchor") and not item["anchor"].get("world_visible", False)
        )
        return CognitiveKnowledgeSnapshot(
            knowledge=knowledge,
            recalled_memories=recalled_memories,
            beliefs=beliefs,
            goals=goals,
            rules=rules,
            # Preserve the attention-layer newest-first invariant. HUD relevance
            # scoring treats index/distance 0 as the current event; reversing here
            # made the oldest visible source event look causally closest.
            current_events=list(attention.recent_newest),
            corpus_references=corpus_references,
            corpus_demand=corpus_demand_meta,
            recall_suppressed=recall_suppressed,
            topology_retrieval=bool(topology_knowledge),
            topology_partial_fallback=bool(legacy_knowledge),
            firewall_suppressed=suppressed,
            anchored_knowledge_count=anchored_knowledge_count,
            visible_world_context_count=visible_world_context_count,
            invisible_anchor_count=invisible_anchor_count,
        )

    async def _flat_character_knowledge(
        self,
        context: HUDContext,
        scorer: RelevanceScorer | None,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT
                ck.instance_id AS evidence_instance_id,
                array_position($1::uuid[], ck.instance_id) - 1 AS instance_depth,
                ck.epistemic_status,
                ck.confidence,
                ck.acquisition_mode,
                ck.source_entity_id,
                ck.first_acquired_at,
                ck.updated_at,
                ck.base_confidence,
                ck.attention_weight,
                ck.trust_weight,
                ck.compatibility_weight,
                ck.retention_weight,
                ck.salience_weight,
                ck.effective_confidence,
                p.proposition_id,
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
                COALESCE(conflicts.items, '[]'::jsonb) AS conflicts
            FROM aios.character_proposition_knowledge ck
            JOIN aios.proposition p ON p.proposition_id=ck.proposition_id
            LEFT JOIN LATERAL (
                SELECT ccr.claim_kind, ccr.predicate_family, ccr.world_id, ccr.dag_node_id
                FROM aios.observation o
                JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
                WHERE o.proposition_id=p.proposition_id
                ORDER BY (ccr.character_instance_id=ck.instance_id) DESC, ccr.resolved_at DESC
                LIMIT 1
            ) ctx ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'proposition_id', other.proposition_id,
                        'text', other.canonical_text,
                        'conflict_type', pc.conflict_type,
                        'strength', pc.strength
                    )
                ) AS items
                FROM aios.proposition_conflict pc
                JOIN aios.proposition other
                  ON other.proposition_id = CASE
                      WHEN pc.proposition_a_id=p.proposition_id THEN pc.proposition_b_id
                      ELSE pc.proposition_a_id
                  END
                JOIN aios.character_proposition_knowledge other_ck
                  ON other_ck.instance_id = ANY($1::uuid[])
                 AND other_ck.proposition_id=other.proposition_id
                WHERE pc.proposition_a_id=p.proposition_id OR pc.proposition_b_id=p.proposition_id
            ) conflicts ON true
            WHERE ck.instance_id = ANY($1::uuid[])
              AND EXISTS (
                  SELECT 1
                  FROM aios.knowledge_acquisition_event kae
                  LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
                  LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
                  LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
                  LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id
                  LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
                  WHERE kae.instance_id=ck.instance_id
                    AND kae.proposition_id=ck.proposition_id
                    AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
              )
            ORDER BY array_position($1::uuid[], ck.instance_id), ck.updated_at DESC
            LIMIT 250
            """,
            list((context.cognitive_instance_ids or context.lineage_instance_ids)),
        )

        result: list[dict[str, Any]] = []
        for rank, row in enumerate(rows):
            item = dict(row)
            item["text"] = item.pop("canonical_text")
            item["conflicts"] = _json_value(item.get("conflicts"), [])
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=(
                    f"{item.get('topic_key','')} {item.get('subject_norm','')} "
                    f"{item.get('predicate_norm','')} {item.get('object_norm','')} {item.get('text','')}"
                ),
                candidate_world_id=context.world_id,
                candidate_entity_id=item.get("source_entity_id"),
                epistemic_status=item.get("epistemic_status"),
                confidence=item.get("effective_confidence") or item.get("confidence"),
                updated_at=item.get("first_acquired_at") or item.get("updated_at"),
            )
            kind = str(item.get("claim_kind") or "BELIEF").upper()
            item["tier"] = self._knowledge_tier(kind, score.total)
            item["relevance"] = score.as_dict()
            result.append(item)

        result.sort(key=lambda x: (x["tier"], -x["relevance"]["total"]))
        return result

    @staticmethod
    def _knowledge_tier(kind: str, score: float) -> int:
        if kind in {"GOAL", "RULE"}:
            return 0
        if kind in {"MEMORY", "RELATIONSHIP", "STATE", "EVENT"} and score >= 1.0:
            return 1
        return 2