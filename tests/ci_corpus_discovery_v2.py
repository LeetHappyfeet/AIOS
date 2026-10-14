"""Disposable PostgreSQL integration smoke for Stage 2 corpus discovery.

Runs AFTER ci_topic_atlas_smoke.py in the canonical cold-start CI database.
Tests vector-only recall, ACL denies, fanwork exclusion and SQL lexical fallback.
"""
import asyncio
from types import SimpleNamespace

from aios_app.semantic_index import corpus_discovery as cold_index
from uuid import UUID

from aios_app.config import settings
from aios_app.db import Database
from aios_app.epistemic.research import CorpusSearchService


class CandidateStub:
    def __init__(self, ids, *, unavailable=False):
        self.ids = ids
        self.unavailable = unavailable

    def search_corpus_discovery(self, query, *, corpus_k=96, topic_k=12):
        if self.unavailable:
            raise RuntimeError("simulated unavailable semantic index")
        return [("corpus",0.91,{"section_id":str(section_id)})
                for section_id in self.ids]


async def _document(db, *, title, content, scopes, idx):
    doc = await db.fetchrow(
        """INSERT INTO aios.corpus_document
           (document_kind,title,content_hash)
           VALUES('document',$1,$2) RETURNING document_id""",
        title,"topic-atlas-stage2-" + str(idx))
    section = await db.fetchrow(
        """INSERT INTO aios.corpus_section
           (document_id,section_order,section_path,heading,content,content_hash)
           VALUES($1,0,'0','Resource notes',$2,$3) RETURNING section_id""",
        doc["document_id"],content,"topic-atlas-stage2-section-"+str(idx))
    for scope in scopes:
        await db.execute(
            """INSERT INTO aios.corpus_document_scope(document_id,scope_key)
               VALUES($1,$2)""",doc["document_id"],scope)
    return doc["document_id"],section["section_id"]


async def main():
    db = Database(settings.db_dsn)
    await db.connect()
    try:
        await db.execute(
            """INSERT INTO aios.corpus_scope(scope_key,display_name,access_class)
               VALUES ('atlas-stage2-public','Public','public'),
                      ('atlas-stage2-restricted','Restricted','restricted'),
                      ('atlas.stage2.fanwork','Fanwork','public')
               ON CONFLICT(scope_key) DO UPDATE
               SET access_class=EXCLUDED.access_class""")
        await db.execute(
            """INSERT INTO aios.character_identity(character_id,display_name)
               VALUES('atlas-stage2-reader','Atlas Reader')
               ON CONFLICT(character_id) DO NOTHING""")
        world = await db.fetchrow(
            """INSERT INTO aios.world(world_key) VALUES('atlas-stage2-world')
               RETURNING world_id""")
        instance = await db.fetchrow(
            """INSERT INTO aios.character_instance(character_id,world_id)
               VALUES('atlas-stage2-reader',$1) RETURNING instance_id""",world["world_id"])
        public, public_section = await _document(
            db,title="Canopy notes",
            content="The woodland canopy supports layered woodland habitats.",
            scopes=["atlas-stage2-public"],idx=1)
        _, restricted_section = await _document(
            db,title="Unreleased field notebook",
            content="An unreleased private note about a woodland discovery.",
            scopes=["atlas-stage2-restricted"],idx=2)
        _, denied_section = await _document(
            db,title="Explicitly denied public note",
            content="A woodland report with explicit subject-level denial.",
            scopes=["atlas-stage2-public","atlas-stage2-restricted"],idx=3)
        _, fan_section = await _document(
            db,title="Fiction story",
            content="Woodland creative writing outside non-fanwork research.",
            scopes=["atlas-stage2-public","atlas.stage2.fanwork"],idx=4)
        # The previous code's broad public grant cannot override a deny row.
        await db.execute(
            """INSERT INTO aios.character_corpus_access
               (character_id,scope_key,allowed)
               VALUES('atlas-stage2-reader','atlas-stage2-restricted',false)
               ON CONFLICT(character_id,scope_key) DO UPDATE SET allowed=false""")
        service = CorpusSearchService(db)
        service.semantic = CandidateStub([restricted_section,denied_section,fan_section,public_section])
        consumed_before = await db.fetchval("SELECT count(*) FROM aios.source_consumption")
        no_lexical = await service.search(
            instance_id=instance["instance_id"],query="photosynthesis",
            include_fanwork=False,limit=8)
        ids = {item.section_id for item in no_lexical.hits}
        assert ids == {public_section},f"vector-only authorized recall should return only public source: {ids}"
        assert no_lexical.hits[0].retrieval_methods == ("semantic",)

        # Stage 2 topic atlas expansion: a topic may discover a relevant
        # accessible passage even without a direct vector/lexical source hit.
        domain_topic = await db.fetchval(
            """SELECT t.topic_id FROM aios.knowledge_topic t
               JOIN aios.knowledge_topic_mention m ON m.topic_id=t.topic_id
               WHERE m.source_kind='corpus_facet'
                 AND m.source_key LIKE '%:subject:Renamon'
                 AND t.namespace='catalog:fiction.topic-atlas-ci'
               LIMIT 1""")
        assert domain_topic is not None
        await db.execute(
            """INSERT INTO aios.corpus_document_domain(document_id,knowledge_domain)
               VALUES($1,'fiction.topic-atlas-ci')""",public)
        await db.execute(
            """INSERT INTO aios.knowledge_topic_source
               (topic_id,document_id,section_id,link_kind,source_key,
                source_revision,similarity,status)
               VALUES ($1,$2,$3,'vector_candidate',$4,'test-vector-v1',0.81,'candidate')""",
            domain_topic,public,public_section,str(public_section))
        class TopicOnlyStub:
            def search_corpus_discovery(self,query,*,corpus_k=96,topic_k=12):
                return [("topic",0.82,{"topic_id":str(domain_topic)})]
        service.semantic = TopicOnlyStub()
        topic_result = await service.search(
            instance_id=instance["instance_id"],query="xylophonic",
            include_fanwork=False,limit=8)
        assert {h.section_id for h in topic_result.hits}=={public_section}
        assert "topic" in topic_result.hits[0].retrieval_methods

        service.semantic = CandidateStub([],unavailable=True)
        fallback = await service.search(
            instance_id=instance["instance_id"],query="woodland",
            include_fanwork=False,limit=8)
        ids = {item.section_id for item in fallback.hits}
        assert ids == {public_section},f"lexical fallback must preserve deny and fanwork exclusions: {ids}"
        assert "lexical" in fallback.hits[0].retrieval_methods
        assert await db.fetchval("SELECT count(*) FROM aios.source_consumption") == consumed_before

        # Exercise the production SQL fingerprint/receipt queries against real
        # PostgreSQL while replacing only external Qdrant and inference IO.
        indexed_points = {}
        deleted = []
        class FakeEmbedder:
            def embed(self,texts):
                assert all("text: " in t for t in texts)
                return [[0.1,0.2,0.3] for _ in texts]
        class FakeClient:
            def delete(self,*,collection_name,points_selector,wait):
                assert collection_name=="corpus_sections_v2" and wait
                deleted.extend(points_selector.points)
        class FakeStore:
            def __init__(self):
                self.client=FakeClient()
            def upsert(self,points):
                for point in points: indexed_points[str(point.id)]=point
        fake=FakeStore()
        cold_index._get_embedder=lambda cfg:FakeEmbedder()
        cold_index._get_store=lambda cfg,name:fake
        cfg=SimpleNamespace(corpus_collection="corpus_sections_v2",
            topic_collection="knowledge_topics_v1",embedding_model="test-model",
            embedding_version="v1")
        assert await cold_index.index_corpus_discovery_once(db,cfg,limit=32)>=4
        assert str(public_section) in indexed_points
        assert await cold_index.index_corpus_discovery_once(db,cfg,limit=32)==0
        previous = await db.fetchval(
            "SELECT source_fingerprint FROM aios.corpus_discovery_projection WHERE section_id=$1",
            public_section)
        await db.execute("UPDATE aios.corpus_section SET heading='Changed heading' WHERE section_id=$1",
                         public_section)
        assert await cold_index.index_corpus_discovery_once(db,cfg,limit=32)==1
        current = await db.fetchval(
            "SELECT source_fingerprint FROM aios.corpus_discovery_projection WHERE section_id=$1",
            public_section)
        assert previous!=current

        await db.execute("DELETE FROM aios.corpus_section WHERE section_id=$1",public_section)
        assert await db.fetchval(
            "SELECT count(*) FROM aios.corpus_discovery_tombstone WHERE section_id=$1",
            public_section)==1
        assert await cold_index.prune_deleted_corpus_once(db,cfg,limit=32)>=1
        assert str(public_section) in [str(value) for value in deleted]
        assert await db.fetchval(
            "SELECT count(*) FROM aios.corpus_discovery_tombstone WHERE section_id=$1",
            public_section)==0
        # No source was materialized and no world assertion was created by search.
        print("Corpus Discovery V2 PostgreSQL smoke PASS: vector-only recall, ACL/fanwork, lexical fallback, real source-fingerprint refresh and deletion queue")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
