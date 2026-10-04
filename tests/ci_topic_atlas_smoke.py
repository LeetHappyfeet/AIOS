"""Real PostgreSQL smoke for canonical cold-start Topic Atlas V1.

Run only on a disposable database AFTER python tests/ci_canonical_cold_start.py.
No Qdrant or Fuseki service required: their IO ordering has separate unit tests.
"""
import asyncio

from aios_app.config import settings
from aios_app.db import Database
from aios_app.topic_atlas.collector import collect_topics_once


async def main():
    db = Database(settings.db_dsn)
    await db.connect()
    try:
        before_world = await db.fetchval("SELECT count(*) FROM aios.world_proposition_assertion")
        parent = await db.fetchrow(
            """INSERT INTO aios.knowledge_domain(domain_key,display_name)
               VALUES ('fiction.topic-atlas-ci','Topic Atlas CI')
               ON CONFLICT(domain_key) DO UPDATE SET display_name=EXCLUDED.display_name
               RETURNING domain_id""")
        child = await db.fetchrow(
            """INSERT INTO aios.knowledge_domain
               (domain_key,display_name,parent_domain_id)
               VALUES ('fiction.topic-atlas-ci.renamon','Renamon topic',
                       $1)
               ON CONFLICT(domain_key) DO UPDATE SET parent_domain_id=EXCLUDED.parent_domain_id
               RETURNING domain_id""",parent["domain_id"])
        doc = await db.fetchrow(
            """INSERT INTO aios.corpus_document
               (document_kind,title,content_hash)
               VALUES ('document','Atlas fixture','topic-atlas-v1-cold-start-fixture')
               RETURNING document_id""")
        section = await db.fetchrow(
            """INSERT INTO aios.corpus_section
               (document_id,section_order,section_path,heading,content,content_hash)
               VALUES ($1,0,'0','Digital ecology',
                       'An example of source text not semantically ingested.',
                       'topic-atlas-v1-section-fixture')
               RETURNING section_id""",doc["document_id"])
        await db.execute(
            """INSERT INTO aios.corpus_document_domain(document_id,knowledge_domain)
               VALUES ($1,'fiction.topic-atlas-ci')""",doc["document_id"])
        await db.execute(
            """INSERT INTO aios.corpus_document_facet(document_id,facet_type,facet_value)
               VALUES ($1,'subject','Renamon')""",doc["document_id"])
        for _ in range(8):
            await collect_topics_once(db, limit=64)
            ready = await db.fetchval(
                """SELECT count(DISTINCT source_kind) FROM aios.knowledge_topic_discovery_receipt
                   WHERE (source_kind='knowledge_domain' AND
                            source_key=ANY($1::text[]))
                      OR (source_kind='corpus_heading' AND source_key=$2)
                      OR (source_kind='corpus_facet' AND source_key=$3)""",
                [str(parent["domain_id"]),str(child["domain_id"])],
                str(section["section_id"]),
                f"{doc['document_id']}:subject:Renamon")
            if ready == 3:
                break
        assert ready == 3, "domain, heading and facet should all be collected"
        assert await db.fetchval(
            """SELECT count(*) FROM aios.knowledge_topic_relation r
               JOIN aios.knowledge_topic_mention c ON c.topic_id=r.source_topic_id
               JOIN aios.knowledge_topic_mention p ON p.topic_id=r.target_topic_id
               WHERE r.relation_kind='broader' AND r.status='verified'
                 AND c.source_kind='knowledge_domain' AND c.source_key=$1
                 AND p.source_kind='knowledge_domain' AND p.source_key=$2""",
            str(child["domain_id"]),str(parent["domain_id"])) == 1
        assert await db.fetchval(
            """SELECT count(*) FROM aios.knowledge_topic_source ts
               JOIN aios.knowledge_topic t ON t.topic_id=ts.topic_id
               WHERE ts.document_id=$1 AND t.namespace='catalog:fiction.topic-atlas-ci'""",
            doc["document_id"]) == 2
        assert await db.fetchval("SELECT count(*) FROM aios.world_proposition_assertion") == before_world

        original = await db.fetchrow(
            """SELECT t.topic_id FROM aios.knowledge_topic t
               JOIN aios.knowledge_topic_mention m ON m.topic_id=t.topic_id
               WHERE m.source_kind='corpus_heading' AND m.source_key=$1""",
            str(section["section_id"]))
        await db.execute("UPDATE aios.corpus_section SET heading='Digital geography' WHERE section_id=$1",
                         section["section_id"])
        for _ in range(4):
            await collect_topics_once(db,limit=64)
            current = await db.fetchrow(
                """SELECT t.topic_id,t.display_label
                   FROM aios.knowledge_topic_mention m
                   JOIN aios.knowledge_topic t ON t.topic_id=m.topic_id
                   WHERE m.source_kind='corpus_heading' AND m.source_key=$1""",
                str(section["section_id"]))
            if current and current["display_label"] == "Digital geography":
                break
        assert current and current["topic_id"] != original["topic_id"]
        assert await db.fetchval(
            "SELECT status FROM aios.knowledge_topic WHERE topic_id=$1",
            original["topic_id"]) == "retired"
        assert await db.fetchval("SELECT count(*) FROM aios.world_proposition_assertion") == before_world
        print("Topic Atlas PostgreSQL smoke PASS: source collection, domain hierarchy, scoped corpus coverage, source revision retirement, no world assertion promotion")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
