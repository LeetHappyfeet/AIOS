"""Typed deterministic retrieval. Query planners never execute storage operations."""
from __future__ import annotations
from typing import Any
from .contracts import InquiryDemand, InquiryEvidence, InquiryHit
from aios_app.hud.context import HUDContextResolver
from aios_app.epistemic.research import CharacterResearchService, research_terms


def _matching(items: list[dict], query: str, limit: int) -> list[dict]:
    terms = set(research_terms(query, limit=12))
    ranked = sorted(items, key=lambda item: len(terms & set(research_terms(
        " ".join(str(item.get(key) or "") for key in (
            "text", "canonical_text", "topic_key", "subject_norm", "predicate_norm", "object_norm"
        )), limit=60))), reverse=True)
    return [item for item in ranked if terms & set(research_terms(str(
        item.get("text") or item.get("canonical_text") or ""), limit=60))][:limit]


class InquiryLookup:
    def __init__(self, db: Any):
        self.db = db

    async def search(self, demand: InquiryDemand, *,
                     query: str | None = None, target: str = "auto",
                     limit: int = 3) -> InquiryEvidence:
        """Enforce scope before any backend read. Source repair cannot use /char."""
        limit = max(1, min(int(limit), 3))
        q = str(query or demand.question).strip()[:240]
        if not q:
            return InquiryEvidence("unresolved", reason="empty_query")
        if target not in {"auto", "source", "personal", "world", "reference"}:
            raise ValueError("invalid inquiry target")
        ctx = await HUDContextResolver(self.db).resolve(demand.instance_id)
        if demand.evidence_scope == "source_local":
            if target not in {"auto", "source"}:
                raise PermissionError("source admission cannot consult character/world/corpus")
            return await self._source(ctx, demand, limit=limit)
        if target == "auto":
            target = {
                "unresolved_reference": "source",
                "attribution_unresolved": "source",
                "episodic_gap": "personal",
                "world_gap": "world",
                "concept_gap": "reference",
                "conflicting_evidence": "world",
            }.get(demand.uncertainty_kind, "personal")
        if target == "source":
            return await self._source(ctx, demand, limit=limit)
        if target == "personal":
            from aios_app.epistemic.cognitive_context import CognitiveContextService
            items = await CognitiveContextService(self.db).lookup_character_knowledge(
                ctx, None, limit=30)
            selected = _matching(items, q, limit)
            hits = tuple(InquiryHit("char", str(row.get("proposition_id") or ""),
                       str(row.get("text") or row.get("canonical_text") or "")[:650],
                       {"instance_id": str(row.get("evidence_instance_id") or demand.instance_id),
                        "source_node_id": str(row.get("source_node_id") or ""),
                        "epistemic_status": str(row.get("epistemic_status") or "")},
                       True) for row in selected)
            return InquiryEvidence("partial" if hits else "unresolved", hits, "personal_recall")
        if target == "world":
            from aios_app.epistemic.world_retrieval import WorldPropositionRetriever
            from aios_app.epistemic.relevance import CognitiveRelevanceScorer
            scored = await WorldPropositionRetriever(self.db).retrieve(
                ctx, CognitiveRelevanceScorer(ctx, focus_text=q),
                query_text=q, limit=limit)
            hits = tuple(InquiryHit("world", str(row.get("proposition_id") or ""),
                         str(row.get("text") or "")[:650],
                         {"world_id": str(row.get("source_world_id") or ""),
                          "source_node_id": str(row.get("source_node_id") or ""),
                          "claim_id": str(row.get("evidence_claim_id") or "")})
                         for row in scored[:limit])
            return InquiryEvidence("partial" if hits else "unresolved", hits, "world_reference")
        found = await CharacterResearchService(self.db).search(
            instance_id=demand.instance_id, query=q, limit=limit, include_fanwork=False)
        hits = tuple(InquiryHit("corpus", str(hit.section_id), hit.excerpt[:650],
                     {"document_id": str(hit.document_id), "research_id": str(found.research_id),
                      "title": hit.title or "", "scopes": list(hit.scopes)}) for hit in found.hits)
        return InquiryEvidence("partial" if hits else "unresolved", hits, found.status)

    async def _source(self, ctx: Any, demand: InquiryDemand, *, limit: int) -> InquiryEvidence:
        if demand.source_node_id is None:
            return InquiryEvidence("unresolved", reason="no_source_anchor")
        # The source timeline is a hard read boundary: never query siblings or
        # descendants of the anchored node, never infer an antecedent by vectors.
        timeline = ctx.source_timeline_id or ctx.timeline_id
        rows = await self.db.fetch(
            """WITH RECURSIVE ancestry AS (
                 SELECT dn.node_id,dn.timeline_id,dn.speaker_id,
                        dn.message_text, 0 AS depth
                   FROM aios.dag_node dn
                  WHERE dn.node_id=$1 AND dn.timeline_id=$2
                 UNION ALL
                 SELECT parent.node_id,parent.timeline_id,parent.speaker_id,
                        parent.message_text,a.depth+1
                   FROM ancestry a
                   JOIN aios.dag_edge e ON e.child_node_id=a.node_id
                                         AND e.timeline_id=$2
                   JOIN aios.dag_node parent ON parent.node_id=e.parent_node_id
                                            AND parent.timeline_id=$2
                  WHERE a.depth < 3
               )
               SELECT DISTINCT ON (node_id) node_id,timeline_id,speaker_id,
                      message_text,depth
                 FROM ancestry ORDER BY node_id,depth""",
            demand.source_node_id, timeline)
        rows = sorted(rows, key=lambda r: int(r["depth"]))
        if not rows or rows[0]["node_id"] != demand.source_node_id:
            return InquiryEvidence("unresolved", reason="source_not_in_active_timeline")
        hits = tuple(InquiryHit(
            "source_dag", str(row["node_id"]),
            str(row["message_text"] or "")[-650:],
            {"timeline_id": str(row["timeline_id"]), "depth": int(row["depth"]),
             "speaker_id": str(row["speaker_id"] or "")}
        ) for row in rows[:limit + 1] if row["message_text"])
        # Returning ancestry is evidence exposure, not a certified referent.
        return InquiryEvidence("partial" if len(hits) > 1 else "unresolved",
                               hits, "bounded_same_timeline_source_ancestry")
