"""Disposable PostgreSQL check: historical bad topics and revision convergence."""
import asyncio
from uuid import uuid4

from aios_app.config import settings
from aios_app.db import Database
from aios_app.topic_atlas.collector import _collect_row
from aios_app.topic_atlas.hygiene import retire_invalid_candidates_once
from aios_app.topic_atlas.diagnostics import atlas_health


async def main():
    db=Database(settings.db_dsn)
    await db.connect()
    try:
        key="ci-atlas-stable:"+str(uuid4())
        row={
            "source_key":key,"source_revision":"revision-a","origin_key":key,
            "label":"Digital ecology","identifier":"Digital ecology",
            "topic_kind":"concept","domain_key":"fiction.digimon",
            "document_id":None,"section_id":None,"instance_id":None,
            "character_id":None,"world_id":None,
        }
        assert await _collect_row(db,"corpus_heading",row)==1
        saved=await db.fetchrow(
            """SELECT t.topic_id,t.vector_revision,t.graph_revision
               FROM aios.knowledge_topic_mention m
               JOIN aios.knowledge_topic t ON t.topic_id=m.topic_id
               WHERE m.source_kind='corpus_heading' AND m.source_key=$1""",key)
        assert saved
        row["source_revision"]="revision-b"
        assert await _collect_row(db,"corpus_heading",row)==1
        refreshed=await db.fetchrow(
            """SELECT vector_revision,graph_revision FROM aios.knowledge_topic
               WHERE topic_id=$1""",saved["topic_id"])
        assert refreshed["graph_revision"]==saved["graph_revision"],(
            saved,refreshed,"non-semantic source revisions must not reproject RDF")
        assert refreshed["vector_revision"]==saved["vector_revision"]

        opaque="a"*64
        bad_key="ci-atlas-opaque:"+str(uuid4())
        bad=await db.fetchrow(
            """INSERT INTO aios.knowledge_topic(
                 topic_key,namespace,topic_kind,normalized_label,display_label,
                 status,visibility)
               VALUES($1,$2,'concept',$3,$3,'candidate','catalog')
               RETURNING topic_id""",bad_key,"catalog:ci_"+str(uuid4()),opaque)
        for i in range(64):
            await retire_invalid_candidates_once(db,limit=16)
            status=await db.fetchval(
                "SELECT status FROM aios.knowledge_topic WHERE topic_id=$1",bad["topic_id"])
            if status=="retired":
                break
        assert status=="retired"
        before=await db.fetchval(
            "SELECT vector_revision FROM aios.knowledge_topic WHERE topic_id=$1",bad["topic_id"])
        await retire_invalid_candidates_once(db,limit=16)
        after=await db.fetchval(
            "SELECT vector_revision FROM aios.knowledge_topic WHERE topic_id=$1",bad["topic_id"])
        assert before==after,"retired candidates must not churn revisions"
        assert await db.fetchval(
            "SELECT to_regclass('aios.character_research_tool_request')") is not None
        snapshot=await atlas_health(db)
        assert "vector_pending" in snapshot["atlas"]
        assert "corpus_sections" in snapshot["coverage"]
        print("Atlas stabilization PostgreSQL PASS: stable effective revisions, durable candidate retirement, idempotent hygiene, tool request schema and health")
    finally:
        await db.close()


if __name__=="__main__":
    asyncio.run(main())
