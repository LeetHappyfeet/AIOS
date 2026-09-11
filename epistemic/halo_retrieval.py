from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from aios_app.hud.context import HUDContext
from aios_app.hud.relevance import HUDRelevanceScorer
from aios_app.epistemic.retrieval import (
    POLICIES,
    RetrievalPolicy,
    TopologyRetriever as BaseTopologyRetriever,
)


logger = logging.getLogger("aios.epistemic.halo")

# The halo is intentionally small.  It expands retrieval context; it is not a
# second recent-message window and its contents are never copied directly into
# the HUD.
HALO_PREDECESSOR_NODES = 4
HALO_MAX_TEXT_CHARS = 2400


class TopologyRetriever(BaseTopologyRetriever):
    """Topology retrieval with a bounded, historically legal DAG context halo.

    The active DAG coordinate remains a hard generation boundary.  Before
    semantic/vector and topology retrieval run, a few predecessor observations
    from the same timeline are added to the retrieval query.  This lets nearby
    context influence semantic recall while preserving all existing /char
    ownership, instance-lineage, branch, and reconciliation constraints.

    The halo never admits future nodes and never walks sideways into sibling
    timelines.  Retrieved knowledge still has to pass the ordinary epistemic
    SQL eligibility and cognitive admission layers before it can reach a HUD.
    """

    def __init__(self, db: Any):
        super().__init__(db)
        self._halo_cache: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}

    async def _dag_halo(
        self,
        context: HUDContext,
    ) -> tuple[str, tuple[str, ...]]:
        # Prefer the immutable source-perception cursor.  Runtime fallback keeps
        # the mechanism useful for non-source-driven agents while remaining on
        # their active timeline.
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
                """
                SELECT dn.node_id, dn.event_id, dn.message_text
                FROM aios.dag_node dn
                JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
                JOIN aios.dag_node head
                  ON head.node_id=$2
                 AND head.timeline_id=$1
                WHERE dn.timeline_id=$1
                  AND dn.event_id < head.event_id
                  AND dn.message_text IS NOT NULL
                  AND btrim(dn.message_text) <> ''
                  AND ie.superseded_at IS NULL
                ORDER BY dn.event_id DESC
                LIMIT $3
                """,
                timeline_id,
                head_node_id,
                HALO_PREDECESSOR_NODES,
            )
        else:
            rows = await self.db.fetch(
                """
                SELECT dn.node_id, dn.event_id, dn.message_text
                FROM aios.dag_node dn
                JOIN aios.dag_node head
                  ON head.node_id=$2
                 AND head.timeline_id=$1
                WHERE dn.timeline_id=$1
                  AND dn.event_id < head.event_id
                  AND dn.message_text IS NOT NULL
                  AND btrim(dn.message_text) <> ''
                ORDER BY dn.event_id DESC
                LIMIT $3
                """,
                timeline_id,
                head_node_id,
                HALO_PREDECESSOR_NODES,
            )

        node_ids = tuple(str(row["node_id"]) for row in rows)
        # Oldest-to-newest keeps the local sequence intelligible to the lexical
        # and embedding query while the current focus remains last/strongest.
        halo_text = " ".join(
            str(row["message_text"]).strip()
            for row in reversed(rows)
            if row.get("message_text")
        )
        if len(halo_text) > HALO_MAX_TEXT_CHARS:
            halo_text = halo_text[-HALO_MAX_TEXT_CHARS:]

        result = (halo_text, node_ids)
        self._halo_cache[cache_key] = result
        if len(self._halo_cache) > 128:
            self._halo_cache.pop(next(iter(self._halo_cache)))
        return result

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
        halo_text, halo_node_ids = await self._dag_halo(context)
        expanded_focus = " ".join(
            part for part in (halo_text, focus_text) if part
        ).strip()

        result = await super().retrieve_character_knowledge(
            context,
            scorer,
            mode=mode,
            focus_text=expanded_focus,
            goals=goals,
            max_hops=max_hops,
            limit=limit,
        )

        # Diagnostics stay internal to the cognition layer.  We intentionally do
        # not inject halo source text into result items or the HUD.
        logger.debug(
            "DAG context halo mode=%s nodes=%s expanded_focus_chars=%d results=%d",
            mode,
            halo_node_ids,
            len(expanded_focus),
            len(result),
        )
        return result


__all__ = [
    "HALO_PREDECESSOR_NODES",
    "HALO_MAX_TEXT_CHARS",
    "POLICIES",
    "RetrievalPolicy",
    "TopologyRetriever",
]
