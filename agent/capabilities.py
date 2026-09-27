from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.hud.context import HUDContextResolver
from aios_app.epistemic.cognitive_context import CognitiveContextService
from aios_app.epistemic.relevance import CognitiveRelevanceScorer
from aios_app.epistemic.scene_state import CharacterSceneStateStore
from aios_app.epistemic.world_retrieval import WorldPropositionRetriever
from aios_app.epistemic.research import CharacterResearchService
from .actions import ActionRegistry, ActionSpec
from .temporal import TemporalTriggerStore


class ResearchRouter:
    """Stable provider boundary for character research."""

    def __init__(self, db: Database):
        self.db = db
        self.corpus = CharacterResearchService(db)

    async def search(
        self, *, instance_id: UUID, query: str, source: str = "auto", limit: int = 8,
    ) -> Mapping[str, Any]:
        provider = str(source or "auto").strip().lower()
        if provider not in {"auto", "corpus"}:
            raise ValueError(f"unsupported research source {provider!r}")
        result = await self.corpus.search(
            instance_id=instance_id, query=query, limit=max(1, min(int(limit), 32)),
        )
        return {
            "provider": "corpus",
            "research_id": str(result.research_id),
            "status": result.status,
            "query": result.query,
            "hits": result.reference_context(),
            "durable_knowledge": False,
        }

    async def read_source(
        self, *, instance_id: UUID, research_id: UUID, section_id: UUID,
    ) -> Mapping[str, Any]:
        # Only a section already exposed by this instance's scoped research
        # event may be read. Knowing a corpus UUID cannot bypass access policy.
        row = await self.db.fetchrow(
            """
            SELECT cs.section_id,cs.document_id,cd.title,cs.heading,cs.content,
                   cce.score,cce.rank,
                   COALESCE(array_agg(DISTINCT cds.scope_key)
                     FILTER (WHERE cds.scope_key IS NOT NULL),'{}') AS scopes
            FROM aios.character_corpus_exposure cce
            JOIN aios.character_research_event cre ON cre.research_id=cce.research_id
            JOIN aios.corpus_section cs ON cs.section_id=cce.section_id
            JOIN aios.corpus_document cd ON cd.document_id=cs.document_id
            LEFT JOIN aios.corpus_document_scope cds ON cds.document_id=cs.document_id
            WHERE cce.research_id=$1 AND cce.section_id=$2 AND cre.instance_id=$3
            GROUP BY cs.section_id,cs.document_id,cd.title,cs.heading,cs.content,
                     cce.score,cce.rank
            """,
            research_id, section_id, instance_id,
        )
        if not row:
            raise PermissionError("source was not exposed to this character research event")
        return {
            "provider": "corpus",
            "research_id": str(research_id),
            "section_id": str(row["section_id"]),
            "document_id": str(row["document_id"]),
            "title": row["title"],
            "heading": row["heading"],
            "text": row["content"],
            "score": row["score"],
            "rank": row["rank"],
            "scopes": list(row["scopes"] or ()),
            "durable_knowledge": False,
        }


def register_agent_capabilities(db: Database, registry: ActionRegistry) -> None:
    contexts = HUDContextResolver(db)
    cognition = CognitiveContextService(db)
    scenes = CharacterSceneStateStore(db)
    research = ResearchRouter(db)
    world = WorldPropositionRetriever(db)
    temporal = TemporalTriggerStore(db)

    async def knowledge_lookup(
        instance_id: UUID, args: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        query = str(args["query"]).strip()
        if not query:
            raise ValueError("knowledge lookup query is empty")
        context = await contexts.resolve(instance_id)
        kinds = tuple(str(v).upper() for v in (args.get("kinds") or []) if str(v).strip())
        limit = max(1, min(int(args.get("limit", 10)), 30))
        scorer = CognitiveRelevanceScorer(context, focus_text=query, goals=())
        candidates = await cognition.lookup_character_knowledge(
            context, scorer, claim_kinds=kinds, limit=limit,
        )
        matches = [
            {
                "text": item.get("text"),
                "claim_kind": item.get("claim_kind"),
                "subject": item.get("subject_norm"),
                "predicate": item.get("predicate_norm"),
                "object": item.get("object_norm"),
                "proposition_id": str(item.get("proposition_id") or ""),
                "source_node_id": str(item.get("source_node_id") or ""),
                "confidence": item.get("effective_confidence", item.get("confidence")),
                "epistemic_status": item.get("epistemic_status"),
                "retrieval_scope": "character",
            }
            for item in candidates
        ]
        return {
            "query": query, "scope": "character",
            "matches": matches, "match_count": len(matches),
        }

    async def world_lookup(
        instance_id: UUID, args: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        query = str(args["query"]).strip()
        if not query:
            raise ValueError("world lookup query is empty")
        context = await contexts.resolve(instance_id)
        domain = str(args.get("domain") or "general")
        kinds = tuple(str(v).upper() for v in (args.get("kinds") or []) if str(v).strip())
        limit = max(1, min(int(args.get("limit", 10)), 30))
        scorer = CognitiveRelevanceScorer(context, focus_text=query, goals=())
        rows = await world.retrieve(
            context, scorer, query_text=query, domain=domain,
            claim_kinds=kinds, limit=limit,
        )
        matches = [
            {
                "text": item.get("text"),
                "claim_kind": item.get("claim_kind"),
                "subject": item.get("subject_norm"),
                "predicate": item.get("predicate_norm"),
                "object": item.get("object_norm"),
                "proposition_id": str(item.get("proposition_id") or ""),
                "source_node_id": str(item.get("source_node_id") or ""),
                "source_world_id": str(item.get("source_world_id") or ""),
                "epistemic_status": item.get("epistemic_status"),
                "retrieval_scope": "world",
                "world_domain": item.get("world_domain"),
            }
            for item in rows
        ]
        return {
            "query": query, "scope": "world", "domain": domain,
            "matches": matches, "match_count": len(matches),
            "durable_character_knowledge": False,
        }

    async def state_inspect(
        instance_id: UUID, args: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        subject = str(args.get("subject") or "runtime").strip().lower()
        context = await contexts.resolve(instance_id)
        if subject == "scene":
            scene = await scenes.current(
                instance_id=instance_id,
                runtime_timeline_id=context.timeline_id,
                runtime_head_node_id=context.head_node_id,
                source_timeline_id=context.source_timeline_id,
                source_head_node_id=context.source_head_node_id,
            )
            return {"subject": "scene", "state_version": context.state_version, "scene": scene}
        if subject == "task":
            row = await db.fetchrow(
                """SELECT ar.state,ar.active_task_id,t.task_type,t.objective,t.status,
                          t.priority,t.result
                   FROM aios.character_agent_runtime ar
                   LEFT JOIN aios.character_cognitive_task t ON t.task_id=ar.active_task_id
                   WHERE ar.instance_id=$1""",
                instance_id,
            )
            return {"subject": "task", "state": dict(row or {})}
        if subject == "goals":
            rows = await db.fetch(
                """SELECT goal_id,goal_text,status,priority,updated_at
                   FROM aios.character_agent_goal
                   WHERE instance_id=$1 AND status='active'
                   ORDER BY priority,updated_at DESC LIMIT 32""",
                instance_id,
            )
            return {"subject": "goals", "goals": [dict(row) for row in rows]}
        if subject != "runtime":
            raise ValueError("state.inspect subject must be runtime, scene, task, or goals")
        return {
            "subject": "runtime",
            "state": {
                "instance_id": str(instance_id),
                "world_id": str(context.world_id),
                "world_key": context.world_key,
                "timeline_id": str(context.timeline_id),
                "head_node_id": str(context.head_node_id or ""),
                "source_timeline_id": str(context.source_timeline_id or ""),
                "source_head_node_id": str(context.source_head_node_id or ""),
                "state_version": context.state_version,
                "lifecycle_state": context.lifecycle_state,
                "location_entity_id": str(context.location_entity_id or ""),
            },
        }

    async def timer_set(
        instance_id: UUID, args: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        duration = args.get("duration_seconds")
        due_raw = args.get("due_at")
        due = datetime.fromisoformat(str(due_raw).replace("Z", "+00:00")) if due_raw else None
        return await temporal.create_timer(
            instance_id=instance_id,
            reason=str(args.get("reason") or ""),
            duration_seconds=int(duration) if duration is not None else None,
            due_at=due,
            priority=int(args.get("priority", 100)),
            dedupe_key=str(args["dedupe_key"]) if args.get("dedupe_key") else None,
        )

    async def timer_cancel(
        instance_id: UUID, args: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await temporal.cancel(
            instance_id=instance_id, trigger_id=UUID(str(args["trigger_id"])),
        )

    async def timer_list(
        instance_id: UUID, args: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        timers = await temporal.list(
            instance_id=instance_id,
            include_terminal=bool(args.get("include_terminal", False)),
            limit=int(args.get("limit", 32)),
        )
        return {"timers": timers, "count": len(timers)}

    async def research_search(
        instance_id: UUID, args: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await research.search(
            instance_id=instance_id, query=str(args["query"]),
            source=str(args.get("source") or "auto"), limit=int(args.get("limit", 8)),
        )

    async def source_read(
        instance_id: UUID, args: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await research.read_source(
            instance_id=instance_id,
            research_id=UUID(str(args["research_id"])),
            section_id=UUID(str(args["section_id"])),
        )

    registry.register(ActionSpec(
        name="knowledge.lookup",
        schema={"type":"object","required":["query"],"properties":{
            "query":{"type":"string"},"limit":{"type":"integer"},
            "kinds":{"type":"array","items":{"type":"string"}}},"additionalProperties":False},
        side_effect_class="read_only",
        allowed_worker_classes=frozenset({"executive","research","planning","reflection"}),
        handler=knowledge_lookup,
        result_mode="return_to_cognition",
        capability_class="lookup",
        description="Search durable character-owned /char knowledge only.",
    ))
    registry.register(ActionSpec(
        name="world.lookup",
        schema={"type":"object","required":["query"],"properties":{
            "query":{"type":"string"},"domain":{"type":"string"},
            "limit":{"type":"integer"},
            "kinds":{"type":"array","items":{"type":"string"}}},"additionalProperties":False},
        side_effect_class="read_only",
        allowed_worker_classes=frozenset({"executive","research","planning","reflection"}),
        handler=world_lookup,
        result_mode="return_to_cognition",
        capability_class="lookup",
        description="Search authorized public /world knowledge without granting character ownership.",
    ))
    registry.register(ActionSpec(
        name="state.inspect",
        schema={"type":"object","properties":{
            "subject":{"type":"string","enum":["runtime","scene","task","goals"]}},
         "additionalProperties":False},
        side_effect_class="read_only",
        allowed_worker_classes=frozenset({"executive","research","planning","reflection","communication"}),
        handler=state_inspect,
        result_mode="return_to_cognition",
        capability_class="lookup",
        description="Inspect bounded current AIOS state without expanding the HUD.",
    ))
    registry.register(ActionSpec(
        name="timer.set",
        schema={"type":"object","required":["reason"],"properties":{
            "reason":{"type":"string"},
            "duration_seconds":{"type":"integer","minimum":1},
            "due_at":{"type":"string"},
            "priority":{"type":"integer"},
            "dedupe_key":{"type":"string"}},
            "oneOf":[{"required":["duration_seconds"]},{"required":["due_at"]}],
            "additionalProperties":False},
        side_effect_class="internal_write",
        allowed_worker_classes=frozenset({"executive","planning","reflection"}),
        handler=timer_set,
        result_mode="return_to_cognition",
        capability_class="time",
        description="Set a durable wall-clock timer that later emits a TIMER_DUE wake.",
    ))
    registry.register(ActionSpec(
        name="timer.cancel",
        schema={"type":"object","required":["trigger_id"],"properties":{
            "trigger_id":{"type":"string"}},"additionalProperties":False},
        side_effect_class="internal_write",
        allowed_worker_classes=frozenset({"executive","planning","reflection"}),
        handler=timer_cancel,
        result_mode="return_to_cognition",
        capability_class="time",
        description="Cancel a scheduled timer owned by this character.",
    ))
    registry.register(ActionSpec(
        name="timer.list",
        schema={"type":"object","properties":{
            "include_terminal":{"type":"boolean"},"limit":{"type":"integer"}},
            "additionalProperties":False},
        side_effect_class="read_only",
        allowed_worker_classes=frozenset({"executive","planning","reflection"}),
        handler=timer_list,
        result_mode="return_to_cognition",
        capability_class="time",
        description="Inspect this character's durable timers.",
    ))
    registry.register(ActionSpec(
        name="research.search",
        schema={"type":"object","required":["query"],"properties":{
            "query":{"type":"string"},"source":{"type":"string","enum":["auto","corpus"]},
            "limit":{"type":"integer"}},"additionalProperties":False},
        side_effect_class="read_only",
        allowed_worker_classes=frozenset({"executive","research","planning"}),
        handler=research_search,
        result_mode="return_to_cognition",
        capability_class="research",
        description="Search permitted research providers; results remain temporary references.",
    ))
    registry.register(ActionSpec(
        name="source.read",
        schema={"type":"object","required":["research_id","section_id"],"properties":{
            "research_id":{"type":"string"},"section_id":{"type":"string"}},
         "additionalProperties":False},
        side_effect_class="read_only",
        allowed_worker_classes=frozenset({"executive","research","planning"}),
        handler=source_read,
        result_mode="return_to_cognition",
        capability_class="research",
        description="Read a source previously exposed by this character's scoped research event.",
    ))
