"""Rebuildable Qdrant and Fuseki projections for the advisory Topic Atlas.

No topic projection writes authoritative /world observations or character beliefs.
Private topic graphs live in named graphs of the char dataset; catalog graphs
live in named graphs of the world dataset, separate from asserted world graphs.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

from qdrant_client.http import models as qm

from aios_app.semantic_index.service import _get_embedder, _get_store

VECTOR_VERSION = "knowledge-topic-vector-v1"
RDF_VERSION = "knowledge-topic-rdf-v1"
MAX_GRAPH_RELATIONS = 96


def _topic_vector_text(row, aliases) -> str:
    terms = sorted({str(alias["display_alias"]) for alias in aliases if alias["display_alias"]})
    return " | ".join(filter(None, [
        "topic: " + str(row["display_label"]),
        "type: " + str(row["topic_kind"]),
        "aliases: " + "; ".join(terms) if terms else "",
        "description: " + str(row["description"]) if row["description"] else "",
    ]))


def topic_graph_location(row) -> tuple[str, str]:
    namespace = quote(str(row["namespace"]), safe="")
    dataset = "char" if row["visibility"] == "private" else "world"
    return dataset, f"urn:aios:topic-atlas:{namespace}:topic:{row['topic_id']}"


def _literal(value) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def topic_rdf_update(row, aliases, relations, *, candidate_source_count: int = 0) -> tuple[str, str, str, str]:
    dataset, graph = topic_graph_location(row)
    subject = f"urn:aios:knowledge-topic:{row['topic_id']}"
    triples = [
        f"<{subject}> <urn:aios:topic#type> {_literal(row['topic_kind'])} .",
        f"<{subject}> <urn:aios:topic#label> {_literal(row['display_label'])} .",
        f"<{subject}> <urn:aios:topic#namespace> {_literal(row['namespace'])} .",
        f"<{subject}> <urn:aios:topic#status> {_literal(row['status'])} .",
        f"<{subject}> <urn:aios:topic#projectionVersion> {_literal(RDF_VERSION)} .",
        f"<{subject}> <urn:aios:topic#publicCandidateSourceCount> \"{max(0,int(candidate_source_count))}\"^^<http://www.w3.org/2001/XMLSchema#integer> .",
    ]
    if row["description"]:
        triples.append(f"<{subject}> <urn:aios:topic#description> {_literal(row['description'])} .")
    for alias in sorted(aliases, key=lambda a: a["normalized_alias"]):
        triples.append(f"<{subject}> <urn:aios:topic#alias> {_literal(alias['display_alias'])} .")
    for relation in sorted(
        relations,
        key=lambda r: (str(r["source_topic_id"]), str(r["target_topic_id"]),r["relation_kind"]),
    ):
        a = f"urn:aios:knowledge-topic:{relation['source_topic_id']}"
        b = f"urn:aios:knowledge-topic:{relation['target_topic_id']}"
        identifier = hashlib.sha256(
            (a + "|" + b + "|" + str(relation["relation_kind"])).encode("utf-8")
        ).hexdigest()
        rel = "urn:aios:topic-relation:" + identifier
        triples.extend((
            f"<{subject}> <urn:aios:topic#hasNavigationRelation> <{rel}> .",
            f"<{rel}> <urn:aios:topic#fromTopic> <{a}> .",
            f"<{rel}> <urn:aios:topic#toTopic> <{b}> .",
            f"<{rel}> <urn:aios:topic#relationKind> {_literal(relation['relation_kind'])} .",
            f"<{rel}> <urn:aios:topic#status> {_literal(relation['status'])} .",
        ))
    canonical = "\n".join(sorted(set(triples)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # One replacement update avoids leaving an empty graph if INSERT fails.
    sparql = (
        f"DELETE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}\n"
        f"INSERT {{ GRAPH <{graph}> {{\n{canonical}\n}} }}\n"
        f"WHERE {{ OPTIONAL {{ GRAPH <{graph}> {{ ?s ?p ?o }} }} }}"
    )
    return dataset, graph, sparql, digest


async def index_topics_once(db, cfg, *, limit: int = 8) -> int:
    """Vectorize stable topic identities; receipts follow acknowledged Qdrant upsert."""
    rows = await db.fetch(
        """SELECT t.* FROM aios.knowledge_topic t
           LEFT JOIN aios.knowledge_topic_projection p ON p.topic_id=t.topic_id
           WHERE t.status<>'retired' AND
             (p.topic_id IS NULL OR p.vector_revision < t.vector_revision
              OR p.embedding_model IS DISTINCT FROM $2
              OR p.embedding_version IS DISTINCT FROM $3
              OR p.vector_collection IS DISTINCT FROM $4)
           ORDER BY t.updated_at,t.topic_id LIMIT $1""",
        max(1,min(int(limit),32)),cfg.embedding_model,cfg.embedding_version,
        cfg.topic_collection)
    if not rows:
        return 0
    descriptions = []
    points = []
    for row in rows:
        aliases = await db.fetch(
            """SELECT display_alias FROM aios.knowledge_topic_alias
               WHERE topic_id=$1 ORDER BY normalized_alias""",row["topic_id"])
        description = _topic_vector_text(row, aliases)
        descriptions.append(description)
    vectors = _get_embedder(cfg).embed(descriptions)
    for row, description, vector in zip(rows, descriptions, vectors):
        points.append(qm.PointStruct(
            id=str(row["topic_id"]),vector=vector,
            payload={
                "object_type":"knowledge_topic",
                "topic_id":str(row["topic_id"]),
                "topic_kind":row["topic_kind"],
                "namespace":row["namespace"],
                "visibility":row["visibility"],
                "owner_character_id":row["owner_character_id"],
                "topic_status":row["status"],
                "vector_hash":hashlib.sha256(description.encode("utf-8")).hexdigest(),
                "embedding_version":cfg.embedding_version,
                "projection_version":VECTOR_VERSION,
            },
        ))
    _get_store(cfg,cfg.topic_collection).upsert(points)
    acknowledged = 0
    for row, description in zip(rows,descriptions):
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        # If identity/alias changed during the embedding request, leave pending.
        result = await db.fetchrow(
            """INSERT INTO aios.knowledge_topic_projection
               (topic_id,vector_revision,vector_hash,embedding_model,
                embedding_version,vector_collection,vector_projected_at)
               SELECT t.topic_id,t.vector_revision,$3,$4,$5,$6,now()
               FROM aios.knowledge_topic t
               WHERE t.topic_id=$1 AND t.vector_revision=$2
               ON CONFLICT(topic_id) DO UPDATE SET
                 vector_revision=EXCLUDED.vector_revision,
                 vector_hash=EXCLUDED.vector_hash,
                 embedding_model=EXCLUDED.embedding_model,
                 embedding_version=EXCLUDED.embedding_version,
                 vector_collection=EXCLUDED.vector_collection,
                 vector_projected_at=now(),last_error=NULL
               RETURNING topic_id""",
            row["topic_id"],row["vector_revision"],digest,cfg.embedding_model,
            cfg.embedding_version,cfg.topic_collection)
        acknowledged += bool(result)
    return acknowledged


async def prune_retired_topic_vectors_once(db, cfg, *, limit: int = 16) -> int:
    """Acknowledge deletion before clearing index receipts; never resurrect retired points."""
    rows=await db.fetch(
        """SELECT t.topic_id,t.vector_revision,p.vector_collection
           FROM aios.knowledge_topic t JOIN aios.knowledge_topic_projection p
             ON p.topic_id=t.topic_id
           WHERE t.status='retired' AND p.vector_collection IS NOT NULL
           ORDER BY t.updated_at,t.topic_id LIMIT $1""",max(1,min(int(limit),64)))
    if not rows:
        return 0
    count=0
    for row in rows:
        collection=row["vector_collection"]
        store=_get_store(cfg,collection)
        store.client.delete(collection_name=collection,
            points_selector=qm.PointIdsList(points=[str(row["topic_id"])]),wait=True)
        # A concurrent revival must not be acknowledged by an old tombstone.
        receipt=await db.fetchrow(
            """UPDATE aios.knowledge_topic_projection p
               SET vector_collection=NULL,vector_revision=$2,vector_projected_at=now()
               FROM aios.knowledge_topic t
               WHERE p.topic_id=$1 AND t.topic_id=p.topic_id
                 AND t.status='retired' AND t.vector_revision=$2
                 AND p.vector_collection=$3
               RETURNING p.topic_id""",row["topic_id"],row["vector_revision"],collection)
        count+=bool(receipt)
    return count


async def project_topics_once(db, fuseki, *, limit: int = 4) -> int:
    """RDF named-graph projection. Never replaces /world or /char authoritative graphs."""
    rows = await db.fetch(
        """SELECT t.* FROM aios.knowledge_topic t
           LEFT JOIN aios.knowledge_topic_projection p ON p.topic_id=t.topic_id
           WHERE (t.status<>'retired' AND
                 (p.topic_id IS NULL OR p.graph_revision < t.graph_revision
                  OR p.graph_dataset IS NULL OR p.graph_iri IS NULL))
              OR (t.status='retired' AND p.graph_iri IS NOT NULL
                  AND p.graph_revision<t.graph_revision)
           ORDER BY t.updated_at,t.topic_id LIMIT $1""",
        max(1,min(int(limit),16)))
    projected = 0
    for row in rows:
        if row["status"]=="retired":
            dataset,graph=topic_graph_location(row)
            fuseki.update(dataset,f"CLEAR SILENT GRAPH <{graph}>")
            acknowledged=await db.fetchrow(
                """UPDATE aios.knowledge_topic_projection p
                   SET graph_revision=$2,graph_hash='retired-empty',
                       graph_projected_at=now(),last_error=NULL
                   FROM aios.knowledge_topic t
                   WHERE p.topic_id=$1 AND t.topic_id=p.topic_id
                     AND t.status='retired' AND t.graph_revision=$2
                   RETURNING p.topic_id""",row["topic_id"],row["graph_revision"])
            projected+=bool(acknowledged)
            continue
        aliases = await db.fetch(
            """SELECT normalized_alias,display_alias FROM aios.knowledge_topic_alias
               WHERE topic_id=$1 ORDER BY normalized_alias""",row["topic_id"])
        relations = await db.fetch(
            """SELECT source_topic_id,target_topic_id,relation_kind,
                      CASE WHEN bool_or(status='verified') THEN 'verified'
                           ELSE 'candidate' END AS status
               FROM aios.knowledge_topic_relation
               WHERE (source_topic_id=$1 OR target_topic_id=$1) AND status<>'rejected'
               GROUP BY source_topic_id,target_topic_id,relation_kind
               ORDER BY source_topic_id,target_topic_id,relation_kind LIMIT $2""",
            row["topic_id"],MAX_GRAPH_RELATIONS)
        # Only an advisory aggregate enters RDF. Never expose IDs of
        # restricted corpus documents through an unrestricted catalog graph.
        candidate_source_count = await db.fetchval(
            """SELECT count(DISTINCT s.document_id)
               FROM aios.knowledge_topic_source s
               WHERE s.topic_id=$1 AND s.status='candidate'
                 AND EXISTS (
                   SELECT 1 FROM aios.corpus_document_scope cds
                   JOIN aios.corpus_scope scope_def ON scope_def.scope_key=cds.scope_key
                     AND scope_def.access_class='public'
                   WHERE cds.document_id=s.document_id)""",row["topic_id"])
        dataset,graph,sparql,digest = topic_rdf_update(
            row,aliases,relations,candidate_source_count=candidate_source_count or 0)
        # Keep the synchronized Fuseki call inside the dedicated topology worker:
        # cancelling a detached network thread could let a stale graph write
        # finish after a newer revision had already been acknowledged.
        fuseki.update(dataset,sparql)
        receipt = await db.fetchrow(
            """INSERT INTO aios.knowledge_topic_projection
               (topic_id,graph_revision,graph_hash,graph_dataset,graph_iri,graph_projected_at)
               SELECT t.topic_id,t.graph_revision,$3,$4,$5,now()
               FROM aios.knowledge_topic t
               WHERE t.topic_id=$1 AND t.graph_revision=$2
               ON CONFLICT(topic_id) DO UPDATE SET
                 graph_revision=EXCLUDED.graph_revision,graph_hash=EXCLUDED.graph_hash,
                 graph_dataset=EXCLUDED.graph_dataset,graph_iri=EXCLUDED.graph_iri,
                 graph_projected_at=now(),last_error=NULL
               RETURNING topic_id""",
            row["topic_id"],row["graph_revision"],digest,dataset,graph)
        projected += bool(receipt)
    return projected
