# AIOS Knowledge Atlas V1 — Topic Atlas Foundation

Status: experimental and additive. The Topic Atlas is advisory, not epistemic authority. This is the revised Stage 1; hybrid corpus discovery and progressive research are stages 2 and 3.

## Collection

The PostgreSQL topic registry represents subjects worth investigating, even when mentioned in ultimately invalid claims. Small independent lanes gather: authored knowledge domains (registered status), cold corpus headings and structured facets, short contextualized claim subject/object surfaces, semantic-frame-v2 resolved entity keys, and observation-associated proposition topic keys. All but authored domain identities remain candidates.

Mentions retain source kind/key/revision, origin, claim and instance, character and world coordinates. Claim, frame and observation mentions from one claim count as one distinct origin. Source revisions are processed transactionally: obsolete links are retracted, orphan topics retire, and later source mentions can revive them. Deleted/disabled source entries are tombstoned. This does not change Integrity V4, belief admission, corpus consumption or world assertions.

Topic identity is stable across repeated detections and partitioned by namespace: catalog, catalog:domain, char:character, world:world_id and unresolved-source. Unresolved sources remain private. Aliases do not assert external identity equivalence. Advisory co-mention relations and registry-authorized parent-domain relations are independent of asserted ontology edges.

## Qdrant

The new knowledge_topics_v1 collection stores topic labels, kinds, aliases and descriptions as dense vectors using the existing warm Semantic Index embedding model. Payload includes topic ID, status, visibility, namespace and optional character owner. It contains no source transcript body. Topic vector revisions are independent of graph revisions. Retired points are labelled retired and must not be presented as active knowledge.

Set AIOS_QDRANT_TOPIC_COLLECTION to override the collection name.

## Fuseki

/topics is a logical named-graph family, NOT an additional Fuseki admin dataset. Catalog topic graphs use the existing world dataset; private/unresolved topic graphs use the existing char dataset. Their IRIs start with urn:aios:topic-atlas: and are isolated from authoritative /world and /char graphs. The graphs contain labels and bounded reified advisory relations, not asserted observations, beliefs or experiences. PostgreSQL retains the complete source and relationship ledger. Projection uses one SPARQL replacement update per graph; successful Fuseki acknowledgment must precede the SQL receipt.

Do not expose unrestricted internal Qdrant topic/corpus query methods to characters or outside clients. Stage 2 must enforce character-specific corpus ACL and epistemic scope in PostgreSQL before source content, restricted labels or topic metadata are returned.

## Runtime

After running python -m aios_app.migrate, normal launch calls topic discovery on a low-frequency, small-budget cadence in Semantic Index. It indexes topic vectors in the background lane. The independent topology worker catches up topic RDF graphs and does not load an embedding model.

Inspect with PostgreSQL:

    SELECT namespace,display_label,topic_kind,status,visibility,vector_revision,graph_revision
    FROM aios.knowledge_topic ORDER BY updated_at DESC LIMIT 50;

    SELECT t.namespace,t.display_label,COUNT(DISTINCT m.origin_key) AS origins
    FROM aios.knowledge_topic t JOIN aios.knowledge_topic_mention m USING(topic_id)
    GROUP BY t.topic_id ORDER BY origins DESC LIMIT 30;

    SELECT t.display_label,s.document_id,s.section_id,s.link_kind
    FROM aios.knowledge_topic_source s JOIN aios.knowledge_topic t USING(topic_id)
    ORDER BY s.created_at DESC LIMIT 30;

    SELECT t.display_label,p.vector_projected_at,p.graph_dataset,p.graph_iri,p.graph_projected_at
    FROM aios.knowledge_topic t LEFT JOIN aios.knowledge_topic_projection p USING(topic_id)
    ORDER BY t.updated_at DESC LIMIT 30;

After an intentional topic-collection wipe, set its SQL vector_revision to 0 and vector_hash to NULL in aios.knowledge_topic_projection to replay only its vector projection. After intentional topic-graph deletion, set graph_revision to 0 and graph_hash to NULL. Never wipe source evidence to rebuild this catalogue.

## Verification and Stage 1 limits

Canonical cold-start CI applies the complete migration chain with production migrator, executes real PostgreSQL tests for domain hierarchy, heading/facet sourcing, revisions and deletion retirement, and runs topic identity and projection-ordering unit tests. No operational Fuseki or Qdrant servers are required by these tests; an end-to-end external-service smoke test remains to be performed against the AIOS1 development VM before enabling broad search.

Topic Atlas V1 does not yet expose character-authorized topic search, auto-reconcile aliases across namespaces, or build research dossiers. RDF navigation is bounded to 96 relation groups per topic graph; PostgreSQL preserves the complete ledger. The current bounded source scans should later be replaced by a more efficient source-change outbox/cursor as the corpus grows.
