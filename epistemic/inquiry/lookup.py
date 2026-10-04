"""Typed deterministic retrieval. Query planners never execute storage operations."""
from __future__ import annotations
from typing import Any
import re
from .passage import select_passage, SELECTOR_VERSION
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
        # Resolve historical coordinates from this instance's persisted
        # cognition receipt, or authorize an active-source node on its own
        # timeline. Never silently substitute the current timeline for history.
        source = await self.db.fetchrow(
            """SELECT dn.timeline_id, dn.message_text,
                      EXISTS(SELECT 1 FROM aios.message_cognitive_commit c
                             WHERE c.instance_id=$1 AND c.node_id=dn.node_id
                               AND c.timeline_id=dn.timeline_id) AS committed
               FROM aios.dag_node dn WHERE dn.node_id=$2""",
            demand.instance_id, demand.source_node_id)
        if not source:
            return InquiryEvidence("unresolved", reason="missing_source_node")
        timeline = source["timeline_id"]
        if (not source["committed"] and
                (timeline != (ctx.source_timeline_id or ctx.timeline_id)
                 or demand.source_node_id != ctx.source_head_node_id)):
            return InquiryEvidence("unresolved", reason="source_not_authorized_for_instance")
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
        anchor = demand.anchor_text
        if not anchor:
            # Manual inquiries may have a quoted clause but no recorded V11
            # span; use the longest quotation present in the original source.
            quoted = re.findall(r"['\"]([^'\"]{12,140})['\"]", demand.question)
            anchor = next((q for q in sorted(quoted, key=len, reverse=True)
                           if q.casefold() in str(rows[0]["message_text"] or "").casefold()), "")
            if not anchor:
                # Restrict fallback to a distinctive source phrase, never a
                # generic full-question vector/lexical similarity guess.
                for phrase in ("start collecting them",):
                    if phrase in demand.question.casefold():
                        anchor = phrase
                        break
        first = select_passage(
            str(rows[0]["message_text"] or ""), anchor_text=anchor,
            source_span=demand.source_span, uncertainty_kind=demand.uncertainty_kind)
        if not first.anchor_verified:
            return InquiryEvidence("unresolved", reason=first.method)
        hits = [InquiryHit(
            "source_dag", str(rows[0]["node_id"]), first.text,
            {"timeline_id": str(timeline), "depth": 0,
             "speaker_id": str(rows[0]["speaker_id"] or ""),
             "start_char": first.start_char, "end_char": first.end_char,
             "anchor_start_char": first.anchor_start_char,
             "anchor_end_char": first.anchor_end_char,
             "anchor_verified": first.anchor_verified,
             "selection_method": first.method,
             "selector_version": SELECTOR_VERSION})]
        # Historical parents are neutral context, not authoritative antecedents.
        for row in rows[1:limit]:
            excerpt = select_passage(str(row["message_text"] or ""), budget=350)
            if excerpt.text:
                hits.append(InquiryHit(
                    "source_dag", str(row["node_id"]), excerpt.text,
                    {"timeline_id": str(timeline), "depth": int(row["depth"]),
                     "speaker_id": str(row["speaker_id"] or ""),
                     "start_char": excerpt.start_char, "end_char": excerpt.end_char,
                     "anchor_verified": False, "selection_method": excerpt.method,
                     "selector_version": SELECTOR_VERSION}))
        return InquiryEvidence("partial", tuple(hits),
                               "anchored_same_timeline_source_ancestry")
