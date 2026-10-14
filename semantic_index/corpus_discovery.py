"""Cold Corpus Discovery V2: dedicated, revision-aware Qdrant index and candidate topic coverage.

Qdrant is a discovery accelerator. Only PostgreSQL corpus ACLs may authorize a
returned source. All topics/coverage derived here are advisory, not knowledge.
"""
from __future__ import annotations

import logging
from uuid import UUID

from qdrant_client.http import models as qm

from aios_app.semantic_index.service import _get_embedder, _get_store

log = logging.getLogger("aios.semantic_index.corpus_discovery")
VERSION = "corpus-discovery-v2"
LINK_VERSION = "corpus-topic-candidate-v1"

# Generate a fingerprint in SQL over exactly the source/metadata used by the
# embedding representation. A metadata change is not masked by an older receipt.
_SOURCE = """
WITH source AS (
 SELECT cs.section_id,cs.document_id,cs.heading,cs.content,cs.section_path,
        cd.title,cd.author,cd.document_kind,
        COALESCE((
          SELECT array_agg(f.facet_type || ': ' || f.facet_value
                           ORDER BY f.facet_type,f.facet_value)
          FROM aios.corpus_document_facet f WHERE f.document_id=cs.document_id
        ), '{}'::text[]) AS facets,
        COALESCE((
          SELECT array_agg(d.knowledge_domain ORDER BY d.knowledge_domain)
          FROM aios.corpus_document_domain d WHERE d.document_id=cs.document_id
        ), '{}'::text[]) AS domains
 FROM aios.corpus_section cs
 JOIN aios.corpus_document cd ON cd.document_id=cs.document_id
 WHERE length(cs.content)>0
), current_source AS (
 SELECT source.*, md5(concat_ws(E'\\x1f',
             coalesce(title,''),coalesce(heading,''),coalesce(author,''),
             array_to_string(facets,E'\\x1e'),
             array_to_string(domains,E'\\x1e'),content)) AS source_fingerprint
 FROM source
)
"""


def corpus_embedding_text(row) -> str:
    """Descriptive fields enter the embedding, not an unrestricted Qdrant payload."""
    return " | ".join(part for part in (
        "document: " + str(row["title"]) if row["title"] else "",
        "heading: " + str(row["heading"]) if row["heading"] else "",
        "author: " + str(row["author"]) if row["author"] else "",
        "facets: " + "; ".join(row["facets"]) if row["facets"] else "",
        "domains: " + "; ".join(row["domains"]) if row["domains"] else "",
        "text: " + str(row["content"]),
    ) if part)


async def index_corpus_discovery_once(db, cfg, *, limit: int = 8) -> int:
    """Acknowledge a point only if its current SQL source revision still matches."""
    rows = await db.fetch(
        _SOURCE + """
        SELECT c.* FROM current_source c
        LEFT JOIN aios.corpus_discovery_projection p ON p.section_id=c.section_id
        WHERE p.section_id IS NULL OR
              p.source_fingerprint IS DISTINCT FROM c.source_fingerprint OR
              p.vector_collection IS DISTINCT FROM $2 OR
              p.embedding_model IS DISTINCT FROM $3 OR
              p.embedding_version IS DISTINCT FROM $4 OR
              p.projector_version IS DISTINCT FROM $5
        ORDER BY c.section_id LIMIT $1
        """,
        max(1,min(int(limit),32)),cfg.corpus_collection,cfg.embedding_model,
        cfg.embedding_version,VERSION,
    )
    if not rows:
        return 0
    vectors = _get_embedder(cfg).embed([corpus_embedding_text(row) for row in rows])
    points = [qm.PointStruct(
        id=str(row["section_id"]), vector=vector,
        payload={
            "object_type":"corpus_section",
            "section_id":str(row["section_id"]),
            "document_id":str(row["document_id"]),
            "source_fingerprint":row["source_fingerprint"],
            "projector_version":VERSION,
            "embedding_version":cfg.embedding_version,
        },
    ) for row,vector in zip(rows,vectors)]
    # A separate collection prevents live-source vectors and cold-source
    # discovery from sharing a retention/refresh lifecycle.
    _get_store(cfg,cfg.corpus_collection).upsert(points)
    written = 0
    for row in rows:
        # A source changed while the embedding model was working: leave pending.
        receipt = await db.fetchrow(
            _SOURCE + """
            INSERT INTO aios.corpus_discovery_projection
                (section_id,source_fingerprint,vector_collection,embedding_model,
                 embedding_version,projector_version,indexed_at)
            SELECT section_id,source_fingerprint,$3,$4,$5,$6,now()
            FROM current_source
            WHERE section_id=$1 AND source_fingerprint=$2
            ON CONFLICT(section_id) DO UPDATE SET
                source_fingerprint=EXCLUDED.source_fingerprint,
                vector_collection=EXCLUDED.vector_collection,
                embedding_model=EXCLUDED.embedding_model,
                embedding_version=EXCLUDED.embedding_version,
                projector_version=EXCLUDED.projector_version,
                indexed_at=now()
            RETURNING section_id""",
            row["section_id"],row["source_fingerprint"],
            cfg.corpus_collection,cfg.embedding_model,cfg.embedding_version,VERSION,
        )
        written += bool(receipt)
    return written


async def prune_deleted_corpus_once(db, cfg, *, limit: int = 8) -> int:
    """A deletion queue prevents ghosts after source sections are removed."""
    rows = await db.fetch(
        """SELECT section_id FROM aios.corpus_discovery_tombstone
           ORDER BY removed_at,section_id LIMIT $1""",max(1,min(int(limit),32)))
    if not rows:
        return 0
    ids = [str(row["section_id"]) for row in rows]
    # The delete must be acknowledged before removing SQL retry work.
    store = _get_store(cfg,cfg.corpus_collection)
    store.client.delete(collection_name=cfg.corpus_collection,
                        points_selector=qm.PointIdsList(points=ids),wait=True)
    await db.execute(
        """DELETE FROM aios.corpus_discovery_tombstone
           WHERE section_id=ANY($1::uuid[])
             AND NOT EXISTS (SELECT 1 FROM aios.corpus_section cs
                             WHERE cs.section_id=aios.corpus_discovery_tombstone.section_id)""",
        [UUID(value) for value in ids])
    return len(ids)


async def link_corpus_topics_once(db, cfg, *, limit: int = 2,
                                  min_score: float = 0.62) -> int:
    """Advisory vector coverage limited to explicit source-domain namespaces.

    Revisit sources when either the corpus embedding or topic-vector epoch
    changes. No vector association is labelled verified or used as corpus ACL.
    """
    epoch = await db.fetchval(
        """SELECT max(p.vector_projected_at)
           FROM aios.knowledge_topic_projection p
           JOIN aios.knowledge_topic t ON t.topic_id=p.topic_id
           WHERE p.vector_collection=$1 AND p.embedding_version=$2
             AND t.visibility='catalog' AND t.status IN ('candidate','registered','organized')""",
        cfg.topic_collection,cfg.embedding_version)
    if epoch is None:
        return 0
    rows = await db.fetch(
        """SELECT p.section_id,p.source_fingerprint,cs.document_id
           FROM aios.corpus_discovery_projection p
           JOIN aios.corpus_section cs ON cs.section_id=p.section_id
           LEFT JOIN aios.corpus_topic_link_state state ON state.section_id=p.section_id
           WHERE p.vector_collection=$2 AND p.embedding_model=$3 AND p.embedding_version=$4
             AND (state.section_id IS NULL
                  OR state.source_fingerprint IS DISTINCT FROM p.source_fingerprint
                  OR state.topic_epoch < $5 OR state.linker_version<>$6)
           ORDER BY state.scanned_at NULLS FIRST,p.section_id LIMIT $1""",
        max(1,min(int(limit),8)),cfg.corpus_collection,cfg.embedding_model,
        cfg.embedding_version,epoch,LINK_VERSION)
    store = _get_store(cfg,cfg.corpus_collection)
    topic_store = _get_store(cfg,cfg.topic_collection)
    linked = 0
    for row in rows:
        try:
            vector = store.vector(str(row["section_id"]))
        except KeyError:
            # A stale receipt after external Qdrant reset: reindex the source.
            await db.execute(
                """DELETE FROM aios.corpus_discovery_projection
                   WHERE section_id=$1 AND source_fingerprint=$2""",
                row["section_id"],row["source_fingerprint"])
            continue
        matches = topic_store.search(
            vector,top_k=12,score_threshold=min_score,
            qdrant_filter=qm.Filter(must=[
                qm.FieldCondition(key="visibility",match=qm.MatchValue(value="catalog")),
                qm.FieldCondition(key="topic_status",
                                  match=qm.MatchAny(any=["candidate","registered","organized"])),
            ]))
        ids = []
        scores = []
        for _,score,payload in matches:
            try:
                ids.append(UUID(str(payload["topic_id"])))
                scores.append(float(score))
            except (KeyError,TypeError,ValueError):
                continue
        # Validate Qdrant's advisory IDs against SQL, domain membership and
        # active topic records; never trust vector payload to authorize a link.
        permissible = await db.fetch(
            """SELECT t.topic_id FROM aios.knowledge_topic t
               JOIN aios.corpus_document_domain domain
                 ON t.namespace='catalog:' || domain.knowledge_domain
               WHERE domain.document_id=$1
                 AND t.topic_id=ANY($2::uuid[]) AND t.visibility='catalog'
                 AND t.status IN ('candidate','registered','organized')""",
            row["document_id"],ids)
        allowed = {r["topic_id"] for r in permissible}
        chosen = [(tid,score) for tid,score in zip(ids,scores) if tid in allowed][:4]
        async with db.connection() as con:
            async with con.transaction():
                await con.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('aios.corpus.topic.links'))")
                current = await con.fetchrow(
                    """SELECT source_fingerprint FROM aios.corpus_discovery_projection
                       WHERE section_id=$1 AND vector_collection=$2""",
                    row["section_id"],cfg.corpus_collection)
                current_epoch = await con.fetchval(
                    """SELECT max(p.vector_projected_at)
                       FROM aios.knowledge_topic_projection p
                       JOIN aios.knowledge_topic t ON t.topic_id=p.topic_id
                       WHERE p.vector_collection=$1 AND p.embedding_version=$2
                         AND t.visibility='catalog'
                         AND t.status IN ('candidate','registered','organized')""",
                    cfg.topic_collection,cfg.embedding_version)
                if (not current or current["source_fingerprint"]!=row["source_fingerprint"]
                        or current_epoch!=epoch):
                    continue
                # Preserve unchanged links so updating one topic vector does
                # not force every source graph to reproject unnecessarily.
                await con.execute(
                    """DELETE FROM aios.knowledge_topic_source
                       WHERE section_id=$1 AND link_kind='vector_candidate'
                         AND (NOT(topic_id=ANY($2::uuid[]))
                              OR source_revision IS DISTINCT FROM $3)""",
                    row["section_id"],[tid for tid,_ in chosen],
                    row["source_fingerprint"])
                for topic_id,score in chosen:
                    await con.execute(
                        """INSERT INTO aios.knowledge_topic_source
                           (topic_id,document_id,section_id,link_kind,
                            source_key,source_revision,similarity,status)
                           VALUES($1,$2,$3,'vector_candidate',$4,$5,$6,'candidate')
                           ON CONFLICT(topic_id,link_kind,source_key)
                           DO UPDATE SET similarity=EXCLUDED.similarity,
                             source_revision=EXCLUDED.source_revision""",
                        topic_id,row["document_id"],row["section_id"],
                        str(row["section_id"]),row["source_fingerprint"],score)
                    linked += 1
                await con.execute(
                    """INSERT INTO aios.corpus_topic_link_state
                       (section_id,source_fingerprint,topic_epoch,linker_version,scanned_at)
                       VALUES($1,$2,$3,$4,now())
                       ON CONFLICT(section_id) DO UPDATE SET
                         source_fingerprint=EXCLUDED.source_fingerprint,
                         topic_epoch=EXCLUDED.topic_epoch,
                         linker_version=EXCLUDED.linker_version,scanned_at=now()""",
                    row["section_id"],row["source_fingerprint"],epoch,LINK_VERSION)
    return linked
