# Corpus reference RDF

Cold corpus sections now have a dedicated advisory RDF projection. This index
records references, not world observations, truth, character identities or acquired
knowledge. Import, indexing and graph publication create no ingestion events,
claims, propositions, acquisitions or access grants. Existing corpus search and
its SQL access checks remain in place; this pass does not switch HUD retrieval to
an unrestricted graph search.

## Deployment and existing material

Run the normal migration chain and restart semantic-index and semantic-topology.
`20261024_01_corpus_reference_rdf.sql` queues existing corpus sections. The index
worker builds up to two sections per ten-second background cadence; topology
publishes one section graph per cycle. No additional process is required.

Cold corpus text has no linguistic projection to reuse. Each new/changed section
therefore receives one background parse using the already required local English
spaCy model. Metadata, domain, scope and namespace edits reuse that cached parse.
No LLM, embedding, public ontology download or remote source fetch is performed.
Parsing is capped at 16,000 characters and 256 nominal candidates per section;
parser input chunks are capped at 1,024 characters with exact offsets preserved.
`parsed_chars` and `truncated` expose incomplete coverage. Explicitly non-English
documents retain metadata and have `parse_status=unsupported_language`, without
running English extraction. Language-unmarked documents use English extraction.

The knowledge-domain summary includes an `rdf` object with total, ready, pending,
failed, truncated, namespace-mismatch and unsupported-language section counts.
Namespace mismatch reports differences between the document namespace and its
domain default; the worker preserves the document namespace rather than silently
rewriting existing provenance. Ready means the latest
queued revision was built and acknowledged in Fuseki, not that every entity or
statement was understood. Failed counts collection errors; transport failures
leave sections pending and are logged by the topology stage.

## Graph and identity boundaries

Graphs reside in the existing `world` dataset under
`urn:aios:corpus:section:<section UUID>`, using `urn:aios:corpus#` predicates.
Dataset placement does not assert world truth. The graph contains:

- Domain metadata and parent-domain links from the registry.
- Document/section links, namespace, source ID/URI, scope labels and source hashes.
- Provisional reference entities, exact mention offsets and source roles.
- Reified relationship evidence, including attribution, negation and modality.

All identifiers are local URNs. Stored source URLs identify provenance and need
not resolve. Character grants remain in SQL and are deliberately not replicated
as graph authorization. Any future reader must shortlist permitted corpus
sections using the existing SQL ACL, require matching current projection
revisions and payload/source hashes, and rehydrate source text before exposure.
Do not expose unrestricted Fuseki queries to characters.

Proper names can share a provisional entity across sections of the same document
and namespace. Identical names in different documents, namespaces, or example
contexts remain separate. Common noun objects remain source-local. No runtime
character registry lookup, owl:sameAs merge, inferred inventory, or cross-document
identity resolution occurs. Relationships are qualified source reports, never
bare owns/locatedAt assertions. An operator-authored `canon.*` namespace describes
provenance; it does not establish canonical truth.

## Definitions, examples and quotations

Document metadata can declare `reference_kind: dictionary` (or `source_type:
dictionary`); dictionary/lexicon source-type facets are also recognized. The
stored Urban Dictionary hostname supplies a dictionary hint without any network
request. Arbitrary domain names do not automatically establish source reliability.

Section metadata supports `content_role`: definition, example, quotation, or
reference_description. Conventional Definition/Meaning, Example/Usage Example,
and Quotation/Quote headings delimit roles within text. Unmarked dictionary text
stays lexical_reference; every dictionary relation remains illustrative_only.
Example and quotation entities have separate local contexts. Other relationships
remain reference_report_only. Structured headings are evidence of source layout,
not verification of meaning or truth.

For dictionary sections, explicit section metadata `term` and optional `sense_id`
create a source-specific sense node. Terms or meanings are not guessed from page
titles. Without that metadata, mentions and context are still indexed. Pages whose
cleaned text lost definition/example boundaries remain lexical_reference until
an operator or source adapter supplies structure; this pass does not reconstruct
missing layout or scrape separate dictionary senses.

## Revision and failure behavior

Section changes and deletions, document metadata, domain/scope/facet membership,
and registry edits invalidate affected projections. Deletion retains a tombstone
until the source graph is cleared. Publication replaces the entire section graph
and advances its SQL receipt only after transport success; retries are idempotent.
A cancellation waits for an in-flight synchronous write before releasing its row
lock, preventing late stale writes. Existing graphs may persist during backfill,
but their SQL revision will be stale and future consumers must reject them.

Malformed sources back off independently for sixty seconds. The existing topology
worker uses a five-second Fuseki transport timeout with no retry. Model/transport
failures do not interrupt ordinary corpus retrieval or establish that a reference
entity is absent. Vocabulary/schema enrichment and RDF-assisted corpus candidate
retrieval remain follow-up work.
