"""Real PostgreSQL smoke for Stage 3, with only external ingestion/vector IO stubbed.

Run after canonical migrator. Assesses dossier ownership, bounded cycles, topic
follow-ups, source text revisions, current ACL, explicit study and idempotence.
"""
import asyncio
from uuid import UUID, uuid4

from aios_app.config import settings
from aios_app.db import Database
from aios_app.epistemic.research import CharacterResearchService
from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
from aios_app.agent.goal_dependencies import GoalKnowledgeDependencyService


class Candidates:
    def __init__(self, section_ids):
        self.ids=section_ids
    def search_corpus_discovery(self, query, *, corpus_k=96, topic_k=12):
        return [("corpus",0.9,{"section_id":str(section)}) for section in self.ids]


class StubStudyResearcher:
    """Retain real search and SQL receipt semantics; don't enqueue ingestion in CI."""
    def __init__(self, db, base):
        self.db=db
        self.base=base
        self.calls=0
    async def search(self,**kwargs):
        return await self.base.search(**kwargs)
    async def acquire(self,*,instance_id,section_ids,mode,dedupe_key_prefix,
                      expected_content_digests):
        self.calls+=1
        ids=[]
        for sid in section_ids:
            row=await self.db.execute_returning_row(
                """INSERT INTO aios.source_consumption
                   (instance_id,document_id,section_id,mode,status,meta,research_dedupe_key)
                   SELECT $1,cs.document_id,cs.section_id,$3,'pending','{}'::jsonb,$4
                   FROM aios.corpus_section cs WHERE cs.section_id=$2
                     AND md5(cs.content)=$5
                   ON CONFLICT (instance_id,research_dedupe_key)
                     WHERE research_dedupe_key IS NOT NULL
                   DO UPDATE SET requested_at=aios.source_consumption.requested_at
                   RETURNING consumption_id""",instance_id,sid,mode,
                f"{dedupe_key_prefix}:{sid}",expected_content_digests[sid])
            assert row,"text revision must be rechecked when consumed"
            ids.append(row["consumption_id"])
        return {"instance_id":instance_id,"mode":mode,"consumption_ids":ids}


async def main():
    db=Database(settings.db_dsn)
    await db.connect()
    try:
        await db.execute(
            """INSERT INTO aios.corpus_scope(scope_key,display_name,access_class)
               VALUES('stage3.open','Stage 3 public','public'),
                     ('stage3.closed','Stage 3 restricted','restricted')
               ON CONFLICT(scope_key) DO UPDATE
               SET access_class=EXCLUDED.access_class""")
        await db.execute(
            """INSERT INTO aios.character_identity(character_id,display_name)
               VALUES('stage3-researcher','Stage 3 researcher')
               ON CONFLICT(character_id) DO NOTHING""")
        world=await db.fetchrow(
            "INSERT INTO aios.world(world_key) VALUES('stage3-smoke') RETURNING world_id")
        instance=await db.fetchrow(
            """INSERT INTO aios.character_instance(character_id,world_id)
               VALUES('stage3-researcher',$1) RETURNING instance_id""",world["world_id"])
        other=await db.fetchrow(
            """INSERT INTO aios.character_instance(character_id,world_id)
               VALUES('stage3-researcher',$1) RETURNING instance_id""",world["world_id"])
        async def document(title,content,scope,ordinal):
            doc=await db.fetchrow(
                """INSERT INTO aios.corpus_document(document_kind,title,content_hash)
                   VALUES('document',$1,$2) RETURNING document_id""",
                title,f"stage3-smoke-doc-{ordinal}")
            sec=await db.fetchrow(
                """INSERT INTO aios.corpus_section(document_id,section_order,heading,
                   section_path,content,content_hash)
                   VALUES($1,0,$2,'0',$3,$4) RETURNING section_id""",
                doc["document_id"],title,content,f"stage3-smoke-section-{ordinal}")
            await db.execute(
                """INSERT INTO aios.corpus_document_scope(document_id,scope_key)
                   VALUES($1,$2)""",doc["document_id"],scope)
            return doc["document_id"],sec["section_id"]
        public_doc, public_section=await document(
            "Digital ecology","s3renamon habitat ecology of the digital ecosystem",
            "stage3.open",1)
        _,private_section=await document(
            "Private evolution notes","s3renamon secrets from a restricted library",
            "stage3.closed",2)
        topic=await db.fetchrow(
            """INSERT INTO aios.knowledge_topic(topic_key,namespace,topic_kind,
                      normalized_label,display_label,status,visibility)
               VALUES($1,'catalog:fiction.digimon','concept',
                      'digital ecology','Digital ecology','registered','catalog')
               RETURNING topic_id""","stage3-ci:"+str(uuid4()))
        await db.execute(
            """INSERT INTO aios.knowledge_topic_source
               (topic_id,document_id,section_id,link_kind,source_key,source_revision)
               VALUES($1,$2,$3,'corpus_heading',$4,'source-v1')""",
            topic["topic_id"],public_doc,public_section,str(public_section))
        base=CharacterResearchService(db)
        base.searcher.semantic=Candidates([private_section,public_section])
        svc=ProgressiveResearchService(db,researcher=base)
        # Real RDF traversal is separately contract-tested; leave external
        # Fuseki service out of this disposable PostgreSQL smoke.
        async def no_rdf_neighbors(*args,**kwargs):
            return ()
        svc._rdf_neighbors=no_rdf_neighbors
        opened=await svc.start(instance_id=instance["instance_id"],
                               question="s3renamon",max_cycles=2,
                               max_sections=4,max_materializations=1)
        dossier_id=UUID(opened["dossier_id"])
        goal=await db.fetchrow(
            """INSERT INTO aios.character_agent_goal(instance_id,goal_text)
               VALUES($1,'Investigate digital ecology') RETURNING goal_id""",
            instance["instance_id"])
        linked=await svc.start(instance_id=instance["instance_id"],
                               question="s3renamon goal inquiry",origin="goal",
                               goal_id=goal["goal_id"],max_cycles=2)
        linked_id=UUID(linked["dossier_id"])
        assert linked["linked_goal_id"]==str(goal["goal_id"])
        assert await db.fetchval(
            """SELECT count(*) FROM aios.character_goal_research_link
               WHERE instance_id=$1 AND goal_id=$2 AND dossier_id=$3""",
            instance["instance_id"],goal["goal_id"],linked_id)==1
        dependencies=GoalKnowledgeDependencyService(db)
        requirement=await dependencies.reconcile(
            instance_id=instance["instance_id"],goal_id=goal["goal_id"],
            question="Find verified digital ecology reference material",
            query_text="digital ecology",requirement_key="reference:ecology",
            demand={"coverage_status":"missing","internal_coverage":0,
                    "retrieval_state":"available","topology_status":"empty",
                    "coverage_source":"established_character_propositions"})
        requirement_id=requirement["requirement_id"]
        scoped=await svc.start(
            instance_id=instance["instance_id"],
            question="s3renamon goal inquiry",origin="goal",
            goal_id=goal["goal_id"],requirement_id=requirement_id,
            max_cycles=2)
        assert scoped["linked_requirement_id"]==str(requirement_id)
        assert UUID(scoped["dossier_id"])==linked_id
        assert await db.fetchval(
            """SELECT count(*) FROM aios.character_goal_research_requirement_link
               WHERE instance_id=$1 AND goal_id=$2 AND requirement_id=$3
                 AND dossier_id=$4""",
            instance["instance_id"],goal["goal_id"],requirement_id,linked_id)==1
        refreshed=await dependencies.reconcile(
            instance_id=instance["instance_id"],goal_id=goal["goal_id"],
            question="Find verified digital ecology reference material",
            query_text="digital ecology",requirement_key="reference:ecology",
            demand={"coverage_status":"partial","internal_coverage":.4,
                    "retrieval_state":"available","coverage_source":"established_character_propositions",
                    "evidence_ids":[str(uuid4())]})
        assert refreshed["requirement_id"]==requirement_id
        assert refreshed["coverage_status"]=="partial"
        displayed=await dependencies.for_goals(
            instance_id=instance["instance_id"],goal_ids=[goal["goal_id"]])
        assert displayed[goal["goal_id"]][0]["dossier_ids"]==[str(linked_id)]
        try:
            await svc.start(instance_id=other["instance_id"],
                            question="foreign requirement",origin="goal",
                            goal_id=goal["goal_id"],requirement_id=requirement_id)
            raise AssertionError("foreign requirement must not cross instance boundaries")
        except PermissionError:
            pass
        repeated=await svc.start(instance_id=instance["instance_id"],
                                 question="S3RENAMON GOAL INQUIRY",origin="goal",
                                 goal_id=goal["goal_id"],max_cycles=2)
        assert UUID(repeated["dossier_id"])==linked_id
        try:
            await svc.start(instance_id=other["instance_id"],
                            question="foreign goal inquiry",origin="goal",
                            goal_id=goal["goal_id"])
            raise AssertionError("foreign instance goal research must be denied")
        except PermissionError:
            pass
        linked_inspection=await svc.inspect(instance_id=instance["instance_id"],
                                            dossier_id=linked_id)
        assert linked_inspection["linked_goal_ids"]==[str(goal["goal_id"])]
        await db.execute(
            "UPDATE aios.character_agent_goal SET status='completed' WHERE goal_id=$1",
            goal["goal_id"])
        inactive=await svc.advance(instance_id=instance["instance_id"],
                                   dossier_id=linked_id,request_id=uuid4())
        assert inactive["status"]=="goal_inactive",inactive
        assert UUID((await svc.start(instance_id=instance["instance_id"],
                                question=" S3RENAMON ",max_cycles=2))["dossier_id"])==dossier_id
        try:
            await svc.inspect(instance_id=other["instance_id"],dossier_id=dossier_id)
            raise AssertionError("foreign instance must be denied")
        except LookupError:
            pass
        first_request=uuid4()
        step=await svc.advance(instance_id=instance["instance_id"],
                               dossier_id=dossier_id,request_id=first_request)
        assert step["new_sources"]==1,step
        assert {ref["section_id"] for ref in step["references"]}=={str(public_section)}
        assert "Digital ecology" in step["follow_up_questions"],step
        retry=await svc.advance(instance_id=instance["instance_id"],
                                dossier_id=dossier_id,request_id=first_request)
        assert retry["claimed"] is False
        inspection=await svc.inspect(instance_id=instance["instance_id"],
                                     dossier_id=dossier_id)
        assert inspection["source_count"]==1
        assert "s3renamon" in inspection["sources"][0]["excerpt"]
        assert len(inspection["questions"])==2
        second=await svc.advance(instance_id=instance["instance_id"],
                                 dossier_id=dossier_id,request_id=uuid4())
        assert second["new_sources"]==0,second
        exhausted=await svc.advance(instance_id=instance["instance_id"],
                                    dossier_id=dossier_id,request_id=uuid4())
        assert exhausted["status"]=="budget_exhausted",exhausted

        # An edited source must be discovered again before it is studied.
        await db.execute(
            """UPDATE aios.corpus_section SET content=content || ' changed'
               WHERE section_id=$1""",public_section)
        try:
            await svc.study(instance_id=instance["instance_id"],dossier_id=dossier_id,
                            section_ids=[public_section],request_id=uuid4())
            raise AssertionError("changed source must be rejected")
        except PermissionError:
            pass
        await db.execute(
            """UPDATE aios.corpus_section SET
               content='s3renamon habitat ecology of the digital ecosystem'
               WHERE section_id=$1""",public_section)

        # An ACL revocation after discovery blocks actual study.
        await db.execute(
            """INSERT INTO aios.character_corpus_access(character_id,scope_key,allowed)
               VALUES('stage3-researcher','stage3.open',false)
               ON CONFLICT(character_id,scope_key) DO UPDATE SET allowed=false""")
        try:
            await svc.study(instance_id=instance["instance_id"],dossier_id=dossier_id,
                            section_ids=[public_section],request_id=uuid4())
            raise AssertionError("revoked ACL must block study")
        except PermissionError:
            pass
        assert not (await svc.inspect(instance_id=instance["instance_id"],
                                      dossier_id=dossier_id))["sources"]
        await db.execute(
            """DELETE FROM aios.character_corpus_access
               WHERE character_id='stage3-researcher' AND scope_key='stage3.open'""")
        stub=StubStudyResearcher(db,base)
        study=ProgressiveResearchService(db,researcher=stub)
        selection_request=uuid4()
        receipt=await study.study(instance_id=instance["instance_id"],
                                  dossier_id=dossier_id,
                                  section_ids=[public_section],request_id=selection_request)
        assert receipt["status"]=="submitted_to_ingestion",receipt
        assert receipt["integrity_admission"]=="not_asserted"
        again=await study.study(instance_id=instance["instance_id"],
                                dossier_id=dossier_id,
                                section_ids=[public_section],request_id=selection_request)
        assert again["status"]=="submitted" and stub.calls==1,again
        assert await db.fetchval(
            """SELECT count(*) FROM aios.source_consumption
               WHERE instance_id=$1 AND research_dedupe_key=$2""",
            instance["instance_id"],f"research-dossier:{dossier_id}:{public_section}")==1
        inspection=await study.inspect(instance_id=instance["instance_id"],
                                       dossier_id=dossier_id)
        assert inspection["submitted_count"]==1
        paused=await study.set_status(instance_id=instance["instance_id"],
                                       dossier_id=dossier_id,status="paused")
        assert paused["status"]=="paused"
        closed=await study.set_status(instance_id=instance["instance_id"],
                                       dossier_id=dossier_id,status="closed")
        assert closed["status"]=="closed"
        print("Stage 3 PostgreSQL smoke PASS: resumable scoped dossier, bounded topic follow-up, idempotent steps, stale-content rejection, ACL revocation, deliberate study, unique receipt")
    finally:
        await db.close()

if __name__=="__main__":
    asyncio.run(main())
