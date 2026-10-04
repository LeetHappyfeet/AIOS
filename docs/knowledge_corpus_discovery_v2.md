# AIOS Knowledge Atlas — Stage 2: Corpus Discovery V2

Status: additive development-branch implementation. This is the middle stage between
Topic Atlas Foundation (Stage 1) and intentional progressive research (Stage 3).

## Purpose

Previously AIOS embedded cold corpus sections into the shared source_sections_v1
collection, but CharacterResearchService used PostgreSQL full-text search without
querying their existing Qdrant vectors. Discovery therefore depended heavily on
the exact words used in a request. Stage 2 makes those cold sources searchable
through both the existing lexical index and a dedicated semantic index, using
Topic Atlas identities to expand source candidates.

**Qdrant is a candidate generator, never an access or epistemic authority.**
No Qdrant payload text, title, private topic label or metadata is returned by
the internal discovery RPC. Every vector/topic candidate ID is independently
verified in the PostgreSQL character corpus ACL before a passage is exposed.

## Collection and revision lifecycle

The new corpus_sections_v2 collection exclusively holds cold corpus sections.
It shares the warm embedding model with the existing semantic service, but has
its own receipt and background budget. The embedding includes document title,
section heading, author, structured facets, declared domain and original source
content. Its Qdrant payload contains IDs and projector/source-revision metadata,
NOT original corpus prose or source titles.

aios.corpus_discovery_projection fingerprints content AND relevant metadata
and records embedding/collection/projector versions. Source revisions, including
metadata-only changes, invalidate the receipt. The worker writes Qdrant first
and records PostgreSQL completion only if the current source fingerprint still
matches the embedded snapshot. A deletion trigger queues section IDs in
aios.corpus_discovery_tombstone; acknowledged Qdrant deletion removes the queue
entry. The old shared-source collection is not deleted during rollout, but
normal/index-once workers now write new corpus vectors into V2 only.

Environment:
- AIOS_QDRANT_CORPUS_COLLECTION=corpus_sections_v2
- AIOS_SEMANTIC_CORPUS_QUERY_TIMEOUT_SECONDS=2.0 (the corpus lookup has a
  separate, bounded, longer deadline than the latency-sensitive HUD query)
- AIOS_SEMANTIC_BACKGROUND_BATCH_SIZE=8 (shared per-lane background budget)

If the V2 collection is deliberately deleted and rebuilt externally, clear
only aios.corpus_discovery_projection to replay the corpus index. Do not delete
PostgreSQL corpus_section or source evidence to rebuild vectors. If the topic
collection is intentionally rebuilt, invalidate its matching topic projection
receipts so coverage links can refresh after vector recovery.

## Topic coverage and RDF

The coverage worker queries knowledge_topics_v1 using an existing corpus-section
vector, and checks any returned topic ID in PostgreSQL against the source
document's explicitly registered domain namespace. It proposes up to four
candidate links per section, with similarity and source revision, in
aios.knowledge_topic_source (link_kind=vector_candidate, status=candidate).

This is advisory: a vector match does not prove a topic is truly covered by
the document, nor grant the character access to the document. Link receipts in
aios.corpus_topic_link_state are tied to the corpus revision and latest catalog
topic-vector epoch. Changes bump only the affected /topics projection revision.
The existing Fuseki Topic Atlas named graphs gain a candidate source-document
COUNT. Restricted section IDs and source content are deliberately not placed
in a public catalog graph. Authoritative /world and /char triples remain untouched.

## Character research: hybrid search

epistemic.research.CorpusSearchService now:
1. Looks up its character and short-circuits when no corpus access route exists.
2. Asks the warm Semantic Index for top cold-corpus and catalog-topic ID
   candidates using one query embedding, with bounded top-K values.
3. Resolves topic IDs, exact name/alias matches, at most one topic relation
   hop, and their heading/facet/vector_candidate source links in PostgreSQL.
4. Applies character corpus ACL, explicit denies and fanwork exclusions to topic
   source expansion BEFORE its candidate cap.
5. Unions those candidates with PostgreSQL full-text matches. Performs final
   character ACL checks inside the scored SQL query, including explicit denies.
6. Uses reciprocal-rank fusion, modest topic/domain/facet bonuses, and up to
   two results per document before filling the remainder of the result budget.
7. Persists reference-only research/exposure records with lexical/semantic/topic
   retrieval-method flags. Discovery does not itself add beliefs or experiences.

A missing, cold or failed Qdrant service degrades to PostgreSQL lexical research.
The existing corpus consumption/learning policy is unchanged: it must still
cross its separate acquisition boundary to create any character knowledge.

## Inspect

    SELECT count(*) corpus_sections FROM aios.corpus_section;
    SELECT vector_collection,count(*) indexed_sections
    FROM aios.corpus_discovery_projection GROUP BY vector_collection;

    SELECT count(*) pending_deletes FROM aios.corpus_discovery_tombstone;

    SELECT t.namespace,t.display_label,count(DISTINCT s.section_id) linked_sections
    FROM aios.knowledge_topic t JOIN aios.knowledge_topic_source s USING(topic_id)
    WHERE s.link_kind='vector_candidate' AND s.status='candidate'
    GROUP BY t.topic_id ORDER BY linked_sections DESC LIMIT 30;

    SELECT count(*) coverage_receipts FROM aios.corpus_topic_link_state;

Queries must not be made against raw corpus collection from externally exposed
client APIs. Only the PostgreSQL-authorized research service returns passages.

## CI and remaining scope

The canonical cold-start workflow compiles new modules and applies migration
20261004_16_corpus_discovery_v2.sql. It runs unit checks for source
representation, acknowledged projection receipts, and sanitized one-embedding
candidate lookup, plus a real PostgreSQL test for vector-only recall, an
explicit deny, restricted source isolation, fanwork exclusion and lexical
fallback. These mock the warm Qdrant server; run the end-to-end indexing and
Fuseki graph catch-up once on a disposable AIOS1 dataset before relying on
the new production-like pipeline.

Not included in Stage 2: automatic publication of candidate topic links,
multi-episode research dossiers, source section consumption decisions, and
broad automatic learning. Those are Stage 3 responsibilities.
