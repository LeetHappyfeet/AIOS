"""Read-only Knowledge Atlas health. No embedding, graph reads, or expensive HUD work."""
from __future__ import annotations

async def atlas_health(db) -> dict:
    topics=await db.fetchrow(
        """SELECT count(*) AS total,
                  count(*) FILTER(WHERE status<>'retired') AS active,
                  count(*) FILTER(WHERE status='candidate') AS candidates,
                  count(*) FILTER(WHERE status='retired') AS retired,
                  count(*) FILTER(
                    WHERE status<>'retired' AND display_label ~* '^[a-f0-9]{32,64}$'
                  ) AS opaque_active,
                  count(*) FILTER(
                    WHERE status<>'retired' AND
                      (p.topic_id IS NULL OR p.vector_revision<t.vector_revision
                       OR p.vector_collection IS NULL)
                  ) AS vector_pending,
                  count(*) FILTER(
                    WHERE status='retired' AND p.vector_collection IS NOT NULL
                  ) AS vector_retirement_pending,
                  count(*) FILTER(
                    WHERE (status<>'retired' AND
                         (p.topic_id IS NULL OR p.graph_revision<t.graph_revision
                          OR p.graph_iri IS NULL))
                       OR (status='retired' AND p.graph_iri IS NOT NULL
                          AND p.graph_revision<t.graph_revision)
                  ) AS rdf_pending,
                  min(t.updated_at) FILTER (
                    WHERE t.status<>'retired' AND
                      (p.topic_id IS NULL OR p.vector_revision<t.vector_revision
                       OR p.vector_collection IS NULL)
                  ) AS oldest_vector_pending,
                  min(t.updated_at) FILTER (
                    WHERE (t.status<>'retired' AND
                      (p.topic_id IS NULL OR p.graph_revision<t.graph_revision
                       OR p.graph_iri IS NULL))
                       OR (t.status='retired' AND p.graph_iri IS NOT NULL
                           AND p.graph_revision<t.graph_revision)
                  ) AS oldest_rdf_pending
           FROM aios.knowledge_topic t
           LEFT JOIN aios.knowledge_topic_projection p USING(topic_id)""")
    coverage=await db.fetchrow(
        """SELECT
              (SELECT count(*) FROM aios.corpus_document) AS corpus_documents,
              (SELECT count(*) FROM aios.corpus_section) AS corpus_sections,
              (SELECT count(*) FROM aios.corpus_discovery_projection) AS corpus_indexed,
              (SELECT count(DISTINCT topic_id)
                 FROM aios.knowledge_topic_source) AS source_linked_topics,
              (SELECT count(*) FROM aios.knowledge_topic_source
                 WHERE link_kind='vector_candidate') AS candidate_coverage_links,
              (SELECT count(*) FROM aios.character_research_dossier
                 WHERE status='open') AS open_dossiers,
              (SELECT count(*) FROM aios.character_research_step
                 WHERE status='running') AS running_research""")
    sources=await db.fetch(
        """SELECT source_kind,count(*) AS receipts,max(processed_at) AS last_seen
           FROM aios.knowledge_topic_discovery_receipt
           GROUP BY source_kind ORDER BY source_kind""")
    return {"atlas":dict(topics),"coverage":dict(coverage),
            "discovery_receipts":[dict(row) for row in sources],
            "interpretation":"topic discovery is advisory; zero corpus sections is a valid cold-reference state"}
