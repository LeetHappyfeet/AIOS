"""Stage 3: bounded progressive research with source-grounded, resumable dossiers.

A dossier is a CHARACTER-SCOPED inquiry ledger. Search results are references;
only explicit study uses the existing ACL-checked corpus consumption path.
Neither 'discovered' nor 'submitted' asserts truth or Integrity V4 approval.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Sequence
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.research import CharacterResearchService, research_terms

MAX_SEARCH_RESULTS = 8
MAX_SELECTED_PER_STEP = 2


def focus_key(question: str, topic_id: UUID | None = None) -> str:
    normal = " ".join(question.casefold().split())
    if not 3 <= len(normal) <= 600 or not research_terms(normal):
        raise ValueError("research question needs searchable subject matter (3-600 characters)")
    return hashlib.sha256(f"{topic_id or ''}\x1f{normal}".encode("utf-8")).hexdigest()


def _serial(row: Any) -> dict[str, Any]:
    if not row:
        return {}
    return {k: (str(v) if isinstance(v, UUID) else v)
            for k, v in dict(row).items()}


class ProgressiveResearchService:
    """One bounded search per cycle; optional explicit study per dossier section."""

    def __init__(self, db: Database, *, researcher: CharacterResearchService | None = None):
        self.db = db
        self.researcher = researcher or CharacterResearchService(db)

    async def start(
        self, *, instance_id: UUID, question: str, topic_id: UUID | None = None,
        origin: str = "manual", max_cycles: int = 8, max_sections: int = 24,
        max_materializations: int = 4,
    ) -> dict[str, Any]:
        question = " ".join(str(question).split())
        identity = focus_key(question, topic_id)
        if origin not in {"manual", "cognition", "goal"}:
            raise ValueError("unsupported research dossier origin")
        max_cycles = max(1, min(int(max_cycles), 16))
        max_sections = max(1, min(int(max_sections), 48))
        max_materializations = max(0, min(int(max_materializations), 8))
        async with self.db.connection() as con:
            async with con.transaction():
                instance = await con.fetchrow(
                    "SELECT character_id FROM aios.character_instance WHERE instance_id=$1",
                    instance_id)
                if not instance:
                    raise LookupError("unknown character instance")
                character = str(instance["character_id"])
                if topic_id is not None:
                    admitted = await con.fetchval(
                        """SELECT EXISTS (
                            SELECT 1 FROM aios.knowledge_topic t
                            WHERE t.topic_id=$1 AND t.status<>'retired'
                              AND (t.visibility='catalog' OR
                                   (t.visibility='private' AND t.owner_character_id=$2))
                        )""", topic_id, character)
                    if not admitted:
                        raise PermissionError("topic unavailable to this character")
                row = await con.fetchrow(
                    """INSERT INTO aios.character_research_dossier
                       (instance_id,character_id,focus_key,question,topic_id,origin,
                        max_cycles,max_sections,max_materializations)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                       ON CONFLICT(instance_id,focus_key) DO UPDATE
                       SET updated_at=aios.character_research_dossier.updated_at
                       RETURNING *""",
                    instance_id,character,identity,question,topic_id,origin,
                    max_cycles,max_sections,max_materializations)
                if row["status"] == "closed":
                    raise ValueError("closed dossier; begin a new research question")
                await con.execute(
                    """INSERT INTO aios.character_research_question
                       (dossier_id,question_key,query_text,origin,source_topic_id,priority)
                       VALUES($1,'initial',$2,'initial',$3,100)
                       ON CONFLICT(dossier_id,question_key) DO NOTHING""",
                    row["dossier_id"],question,topic_id)
        return _serial(row)

    async def _owned(self, con, instance_id: UUID, dossier_id: UUID, *, lock: bool = False):
        suffix = " FOR UPDATE" if lock else ""
        row = await con.fetchrow(
            """SELECT * FROM aios.character_research_dossier
               WHERE dossier_id=$1 AND instance_id=$2""" + suffix,
            dossier_id,instance_id)
        if not row:
            raise LookupError("dossier not found for character instance")
        return row

    async def inspect(self, *, instance_id: UUID, dossier_id: UUID) -> dict[str, Any]:
        async with self.db.connection() as con:
            dossier = await self._owned(con,instance_id,dossier_id)
            questions = await con.fetch(
                """SELECT question_id,query_text,origin,status,priority,source_topic_id,
                          created_at,explored_at
                   FROM aios.character_research_question
                   WHERE dossier_id=$1 ORDER BY priority DESC,created_at LIMIT 20""",
                dossier_id)
            # A dossier is scoped to its owning character, but corpus permissions
            # can be revoked later. Surface source IDs only through the SAME
            # public/domain/explicit-grant and explicit-deny ACL as study.
            sources = await con.fetch(
                """SELECT s.section_id,s.document_id,s.status,s.best_score,
                          s.retrieval_methods,s.first_seen_at,s.last_seen_at,
                          doc.title,section.heading,left(section.content,600) AS excerpt,
                          sc.consumption_id,sc.status AS materialization_status
                   FROM aios.character_research_source s
                   JOIN aios.corpus_section section ON section.section_id=s.section_id
                   JOIN aios.corpus_document doc ON doc.document_id=s.document_id
                   LEFT JOIN aios.character_research_materialization sc
                     ON sc.dossier_id=s.dossier_id AND sc.section_id=s.section_id
                   WHERE s.dossier_id=$1
                     AND (
                       EXISTS (
                         SELECT 1 FROM aios.corpus_document_scope scope_link
                         JOIN aios.corpus_scope scdef ON scdef.scope_key=scope_link.scope_key
                           AND scdef.access_class='public'
                         WHERE scope_link.document_id=s.document_id)
                       OR EXISTS (
                         SELECT 1 FROM aios.corpus_document_domain dom
                         JOIN aios.character_knowledge_domain kd
                           ON kd.character_id=$2 AND kd.enabled
                          AND kd.knowledge_domain=dom.knowledge_domain
                         WHERE dom.document_id=s.document_id)
                       OR EXISTS (
                         SELECT 1 FROM aios.corpus_document_collection dc
                         JOIN aios.corpus_source_profile profile
                           ON profile.profile_id=dc.profile_id AND profile.enabled
                         JOIN aios.corpus_scope scope_def ON scope_def.scope_key=profile.scope_key
                           AND scope_def.access_class='domain'
                         JOIN aios.character_knowledge_domain kd
                           ON kd.character_id=$2 AND kd.enabled
                          AND kd.knowledge_domain=profile.knowledge_domain
                         WHERE dc.document_id=s.document_id)
                       OR EXISTS (
                         SELECT 1 FROM aios.corpus_document_scope scopes
                         JOIN aios.character_corpus_access grants
                           ON grants.character_id=$2 AND grants.allowed
                          AND (scopes.scope_key=grants.scope_key OR
                               left(scopes.scope_key,length(grants.scope_key)+1)=grants.scope_key||'.')
                         WHERE scopes.document_id=s.document_id)
                     ) AND NOT EXISTS (
                       SELECT 1 FROM aios.corpus_document_scope denied
                       JOIN aios.character_corpus_access rules ON rules.character_id=$2
                         AND NOT rules.allowed
                        AND (denied.scope_key=rules.scope_key OR
                             left(denied.scope_key,length(rules.scope_key)+1)=rules.scope_key||'.')
                       WHERE denied.document_id=s.document_id)
                   ORDER BY s.best_score DESC,s.last_seen_at DESC LIMIT 48""",
                dossier_id,dossier["character_id"])
            count = await con.fetchrow(
                """SELECT count(*) AS total,
                      count(*) FILTER(WHERE status='submitted') AS submitted
                   FROM aios.character_research_source WHERE dossier_id=$1""",
                dossier_id)
        return {
            "dossier":_serial(dossier),
            "questions":[_serial(q) for q in questions],
            "sources":[_serial(s) for s in sources],
            "source_count":int(count["total"] or 0),
            "submitted_count":int(count["submitted"] or 0),
            "source_visibility":"current_corpus_acl",
        }

    async def list_dossiers(self, *, instance_id: UUID, limit: int = 12) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """SELECT dossier_id,question,topic_id,origin,status,cycles_completed,
                      max_cycles,created_at,updated_at
               FROM aios.character_research_dossier WHERE instance_id=$1
               ORDER BY updated_at DESC LIMIT $2""",instance_id,max(1,min(int(limit),24)))
        return [_serial(row) for row in rows]

    async def set_status(self, *, instance_id: UUID, dossier_id: UUID,
                         status: str) -> dict[str, Any]:
        if status not in {"open","paused","closed"}:
            raise ValueError("unsupported dossier status")
        async with self.db.connection() as con:
            async with con.transaction():
                dossier = await self._owned(con,instance_id,dossier_id,lock=True)
                if dossier["status"] == "closed" and status != "closed":
                    raise ValueError("closed dossiers cannot be reopened")
                if status != "open" and await con.fetchval(
                    """SELECT EXISTS (SELECT 1 FROM aios.character_research_step
                       WHERE dossier_id=$1 AND status='running' AND lease_expires_at>now())""",
                    dossier_id):
                    raise ValueError("cannot pause or close a running research cycle")
                if status == "closed" and await con.fetchval(
                    """SELECT EXISTS (SELECT 1 FROM aios.character_research_selection
                       WHERE dossier_id=$1 AND status='pending' AND lease_expires_at>now())""",
                    dossier_id):
                    raise ValueError("cannot close a pending source selection")
                row = await con.fetchrow(
                    """UPDATE aios.character_research_dossier SET status=$2,updated_at=now()
                       WHERE dossier_id=$1 RETURNING *""",dossier_id,status)
        return _serial(row)

    async def add_question(self, *, instance_id: UUID, dossier_id: UUID,
                           question: str) -> dict[str, Any]:
        question = " ".join(str(question).split())
        key = "manual:" + focus_key(question)
        async with self.db.connection() as con:
            async with con.transaction():
                dossier = await self._owned(con,instance_id,dossier_id,lock=True)
                if dossier["status"] != "open":
                    raise ValueError("only open dossiers accept new questions")
                n = await con.fetchval(
                    """SELECT count(*) FROM aios.character_research_question
                       WHERE dossier_id=$1""",dossier_id)
                if n >= dossier["max_cycles"]:
                    raise ValueError("research question budget exhausted")
                row = await con.fetchrow(
                    """INSERT INTO aios.character_research_question
                       (dossier_id,question_key,query_text,origin,priority)
                       VALUES($1,$2,$3,'manual',90)
                       ON CONFLICT(dossier_id,question_key) DO UPDATE SET
                         query_text=aios.character_research_question.query_text
                       RETURNING question_id,query_text,status""",
                    dossier_id,key,question)
        return _serial(row)

    async def _claim_advance(
        self, *, instance_id: UUID, dossier_id: UUID, request_id: UUID
    ) -> dict[str, Any]:
        async with self.db.connection() as con:
            async with con.transaction():
                dossier = await self._owned(con,instance_id,dossier_id,lock=True)
                previous = await con.fetchrow(
                    """SELECT * FROM aios.character_research_step
                       WHERE dossier_id=$1 AND request_id=$2""",dossier_id,request_id)
                if previous:
                    return {"claimed":False,"step":_serial(previous)}
                if dossier["status"] != "open":
                    return {"claimed":False,"status":dossier["status"]}
                # Failed/crashed work requeues its question, but retains the
                # failed step and consumes one attempt in the hard cycle budget.
                expired = await con.fetch(
                    """UPDATE aios.character_research_step
                       SET status='failed',error='lease_expired',completed_at=now()
                       WHERE dossier_id=$1 AND status='running' AND lease_expires_at<now()
                       RETURNING question_id""",dossier_id)
                if expired:
                    await con.execute(
                        """UPDATE aios.character_research_question SET status='queued'
                           WHERE question_id=ANY($1::uuid[]) AND status='running'""",
                        [r["question_id"] for r in expired])
                if await con.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM aios.character_research_step
                       WHERE dossier_id=$1 AND status='running')""",dossier_id):
                    return {"claimed":False,"status":"busy"}
                attempts = await con.fetchval(
                    "SELECT count(*) FROM aios.character_research_step WHERE dossier_id=$1",
                    dossier_id)
                n_sources = await con.fetchval(
                    "SELECT count(*) FROM aios.character_research_source WHERE dossier_id=$1",
                    dossier_id)
                if attempts >= dossier["max_cycles"] or n_sources >= dossier["max_sections"]:
                    return {"claimed":False,"status":"budget_exhausted"}
                question = await con.fetchrow(
                    """SELECT * FROM aios.character_research_question
                       WHERE dossier_id=$1 AND status='queued'
                       ORDER BY priority DESC,created_at,question_id LIMIT 1 FOR UPDATE""",
                    dossier_id)
                if not question:
                    return {"claimed":False,"status":"no_open_questions"}
                await con.execute(
                    "UPDATE aios.character_research_question SET status='running' WHERE question_id=$1",
                    question["question_id"])
                step = await con.fetchrow(
                    """INSERT INTO aios.character_research_step
                       (dossier_id,question_id,request_id,query_text)
                       VALUES($1,$2,$3,$4) RETURNING *""",
                    dossier_id,question["question_id"],request_id,question["query_text"])
                return {"claimed":True,"step":_serial(step),
                        "dossier":dict(dossier),"question":dict(question),
                        "remaining_sections":dossier["max_sections"]-n_sources}

    async def advance(self, *, instance_id: UUID, dossier_id: UUID,
                      request_id: UUID, include_fanwork: bool = False) -> dict[str, Any]:
        claim = await self._claim_advance(
            instance_id=instance_id,dossier_id=dossier_id,request_id=request_id)
        if not claim["claimed"]:
            return {"dossier_id":str(dossier_id),**claim}
        step = claim["step"]
        question = claim["question"]
        try:
            result = await self.researcher.search(
                instance_id=instance_id,query=question["query_text"],
                limit=min(MAX_SEARCH_RESULTS,claim["remaining_sections"]),
                include_fanwork=include_fanwork)
            # CharacterResearchService.search() already SQL-authorized and
            # records exposure; reject a mismatched research event as defense in depth.
            evidence = await self.db.fetchval(
                """SELECT 1 FROM aios.character_research_event
                   WHERE research_id=$1 AND instance_id=$2""",
                result.research_id,instance_id)
            if not evidence:
                raise PermissionError("research event not owned by dossier instance")
            async with self.db.connection() as con:
                async with con.transaction():
                    locked = await self._owned(con,instance_id,dossier_id,lock=True)
                    latest = await con.fetchrow(
                        """SELECT status,lease_expires_at FROM aios.character_research_step
                           WHERE step_id=$1 FOR UPDATE""",UUID(step["step_id"]))
                    if not latest or latest["status"]!="running" or latest["lease_expires_at"]<=await con.fetchval("SELECT now()"):
                        return {"dossier_id":str(dossier_id),"status":"expired","step_id":step["step_id"]}
                    known = {r["section_id"] for r in await con.fetch(
                        "SELECT section_id FROM aios.character_research_source WHERE dossier_id=$1",
                        dossier_id)}
                    new_count = 0
                    accepted_sections = []
                    for hit in result.hits:
                        if hit.section_id not in known:
                            if len(known)>=locked["max_sections"]:
                                continue
                            known.add(hit.section_id)
                            new_count+=1
                        accepted_sections.append(hit.section_id)
                        await con.execute(
                            """INSERT INTO aios.character_research_source
                               (dossier_id,section_id,document_id,first_research_id,
                                last_research_id,best_score,retrieval_methods)
                               VALUES($1,$2,$3,$4,$4,$5,$6::text[])
                               ON CONFLICT(dossier_id,section_id) DO UPDATE
                               SET last_research_id=EXCLUDED.last_research_id,
                                   best_score=GREATEST(aios.character_research_source.best_score,
                                                       EXCLUDED.best_score),
                                   retrieval_methods=EXCLUDED.retrieval_methods,
                                   last_seen_at=now()""",
                            dossier_id,hit.section_id,hit.document_id,result.research_id,
                            hit.score,list(hit.retrieval_methods))
                    await con.execute(
                        """UPDATE aios.character_research_step
                           SET status='completed',research_id=$2,source_count=$3,
                               newly_discovered=$4,completed_at=now()
                           WHERE step_id=$1 AND status='running'""",
                        UUID(step["step_id"]),result.research_id,len(result.hits),new_count)
                    await con.execute(
                        """UPDATE aios.character_research_question
                           SET status='explored',explored_at=now()
                           WHERE question_id=$1""",question["question_id"])
                    await con.execute(
                        """UPDATE aios.character_research_dossier SET
                           cycles_completed=cycles_completed+1,updated_at=now()
                           WHERE dossier_id=$1""",dossier_id)
                    followups = await self._seed_followups(
                        con,dossier=locked,allowed_sections=accepted_sections)
            return {
                "dossier_id":str(dossier_id),"step_id":step["step_id"],
                "status":result.status,"research_id":str(result.research_id),
                "source_count":len(result.hits),"new_sources":new_count,
                "references":result.reference_context(),
                "follow_up_questions":followups,
                "durable_knowledge":False,
            }
        except Exception as exc:
            async with self.db.connection() as con:
                async with con.transaction():
                    await self._owned(con,instance_id,dossier_id,lock=True)
                    changed = await con.fetchrow(
                        """UPDATE aios.character_research_step
                           SET status='failed',error=$2,completed_at=now()
                           WHERE step_id=$1 AND status='running'
                           RETURNING question_id""",
                        UUID(step["step_id"]),str(exc)[:900])
                    if changed:
                        await con.execute(
                            """UPDATE aios.character_research_question SET status='queued'
                               WHERE question_id=$1 AND status='running'""",
                            changed["question_id"])
            raise

    async def _seed_followups(self, con, *, dossier, allowed_sections: Sequence[UUID]) -> list[str]:
        """One-hop topic expansion only from sections this character just saw."""
        if not allowed_sections:
            return []
        count = await con.fetchval(
            "SELECT count(*) FROM aios.character_research_question WHERE dossier_id=$1",
            dossier["dossier_id"])
        remaining = dossier["max_cycles"]-count
        if remaining<=0:
            return []
        rows = await con.fetch(
            """SELECT DISTINCT ON (t.topic_id)
                      t.topic_id,t.display_label,s.section_id
               FROM aios.knowledge_topic_source s
               JOIN aios.knowledge_topic t ON t.topic_id=s.topic_id
               WHERE s.section_id=ANY($1::uuid[]) AND s.status<>'rejected'
                 AND t.visibility='catalog' AND t.status IN ('candidate','registered','organized')
                 AND t.topic_id IS DISTINCT FROM $2::uuid
                 AND ($2::uuid IS NULL OR EXISTS (
                   SELECT 1 FROM aios.knowledge_topic_relation r
                   WHERE r.status IN ('candidate','verified') AND
                     ((r.source_topic_id=$2 AND r.target_topic_id=t.topic_id) OR
                      (r.target_topic_id=$2 AND r.source_topic_id=t.topic_id))))
                 AND NOT EXISTS(
                   SELECT 1 FROM aios.character_research_question q
                   WHERE q.dossier_id=$3 AND q.source_topic_id=t.topic_id)
               ORDER BY t.topic_id,s.section_id LIMIT $4""",
            list(allowed_sections),dossier["topic_id"],dossier["dossier_id"],
            min(3,remaining))
        questions = []
        for row in rows:
            label=" ".join(str(row["display_label"]).split())[:112]
            if not research_terms(label):
                continue
            inserted = await con.fetchrow(
                """INSERT INTO aios.character_research_question
                   (dossier_id,question_key,query_text,origin,source_topic_id,
                    source_section_id,priority)
                   VALUES($1,$2,$3,'topic_followup',$4,$5,60)
                   ON CONFLICT(dossier_id,question_key) DO NOTHING
                   RETURNING query_text""",
                dossier["dossier_id"],"topic:"+str(row["topic_id"]),label,
                row["topic_id"],row["section_id"])
            if inserted:
                questions.append(inserted["query_text"])
        return questions

    async def study(
        self, *, instance_id: UUID, dossier_id: UUID, section_ids: Sequence[UUID],
        request_id: UUID,
    ) -> dict[str, Any]:
        sections=list(dict.fromkeys(section_ids))
        if not 1<=len(sections)<=MAX_SELECTED_PER_STEP:
            raise ValueError("study selects one or two distinct source sections per request")
        async with self.db.connection() as con:
            async with con.transaction():
                dossier=await self._owned(con,instance_id,dossier_id,lock=True)
                previous=await con.fetchrow(
                    """SELECT * FROM aios.character_research_selection
                       WHERE dossier_id=$1 AND request_id=$2 FOR UPDATE""",
                    dossier_id,request_id)
                if previous and previous["status"]!="pending":
                    return {"status":previous["status"],"selection_id":str(previous["selection_id"]),
                            "result":dict(previous["result"])}
                if previous and previous["lease_expires_at"] > await con.fetchval("SELECT now()"):
                    return {"status":"pending","selection_id":str(previous["selection_id"])}
                if dossier["status"]!="open":
                    raise ValueError("study requires an open dossier")
                source_rows=await con.fetch(
                    """SELECT s.section_id,s.status,s.last_research_id
                       FROM aios.character_research_source s
                       JOIN aios.character_corpus_exposure e
                         ON e.research_id=s.last_research_id AND e.section_id=s.section_id
                       JOIN aios.character_research_event r
                         ON r.research_id=e.research_id AND r.instance_id=$3
                       WHERE s.dossier_id=$1 AND s.section_id=ANY($2::uuid[])""",
                    dossier_id,sections,instance_id)
                if {r["section_id"] for r in source_rows} != set(sections):
                    raise PermissionError("study requires a source actually exposed in this dossier")
                reserved=await con.fetch(
                    """SELECT section_id,status,selection_id
                       FROM aios.character_research_materialization
                       WHERE dossier_id=$1 FOR UPDATE""",dossier_id)
                by_section={r["section_id"]:r for r in reserved}
                if any(by_section.get(section) and by_section[section]["status"]=="submitted"
                       for section in sections):
                    raise ValueError("source already submitted for study")
                if any(by_section.get(section) and by_section[section]["status"]=="pending"
                       and (not previous or by_section[section]["selection_id"]!=previous["selection_id"])
                       for section in sections):
                    raise ValueError("source already reserved by another study")
                total=sum(r["status"] in {"pending","submitted"} and
                          r["section_id"] not in sections for r in reserved)
                if total+len(sections)>dossier["max_materializations"]:
                    raise ValueError("dossier materialization budget exhausted")
                if previous:
                    selection=await con.fetchrow(
                        """UPDATE aios.character_research_selection
                           SET lease_expires_at=now()+interval '2 minutes'
                           WHERE selection_id=$1 RETURNING *""",previous["selection_id"])
                else:
                    selection=await con.fetchrow(
                        """INSERT INTO aios.character_research_selection(dossier_id,request_id)
                           VALUES($1,$2) RETURNING *""",dossier_id,request_id)
                for section in sections:
                    await con.execute(
                        """INSERT INTO aios.character_research_materialization
                           (dossier_id,section_id,selection_id,status)
                           VALUES($1,$2,$3,'pending')
                           ON CONFLICT(dossier_id,section_id) DO UPDATE SET
                             selection_id=EXCLUDED.selection_id,status='pending',updated_at=now()
                           WHERE aios.character_research_materialization.status='failed'""",
                        dossier_id,section,selection["selection_id"])
        try:
            # This call rechecks the CURRENT character corpus ACL, then emits
            # normal source observations with stable IDs on recovery/retry.
            consumed=await self.researcher.acquire(
                instance_id=instance_id,section_ids=sections,mode="research",
                dedupe_key_prefix=f"research-dossier:{dossier_id}")
            ids=list(consumed["consumption_ids"])
            if len(ids)!=len(sections):
                raise RuntimeError("missing source-consumption receipt")
            result={"consumption_ids":[str(value) for value in ids],
                    "section_ids":[str(value) for value in sections],
                    "status":"submitted_to_ingestion",
                    "integrity_admission":"not_asserted"}
            async with self.db.connection() as con:
                async with con.transaction():
                    await self._owned(con,instance_id,dossier_id,lock=True)
                    for section,consumption_id in zip(sections,ids):
                        await con.execute(
                            """UPDATE aios.character_research_materialization
                               SET status='submitted',consumption_id=$4,updated_at=now()
                               WHERE dossier_id=$1 AND section_id=$2 AND selection_id=$3""",
                            dossier_id,section,selection["selection_id"],consumption_id)
                        await con.execute(
                            """UPDATE aios.character_research_source SET status='submitted'
                               WHERE dossier_id=$1 AND section_id=$2""",dossier_id,section)
                    await con.execute(
                        """UPDATE aios.character_research_selection
                           SET status='submitted',result=$2::jsonb,completed_at=now()
                           WHERE selection_id=$1""",selection["selection_id"],json.dumps(result))
                    await con.execute(
                        "UPDATE aios.character_research_dossier SET updated_at=now() WHERE dossier_id=$1",
                        dossier_id)
            return {"selection_id":str(selection["selection_id"]),**result}
        except Exception as exc:
            async with self.db.connection() as con:
                async with con.transaction():
                    await self._owned(con,instance_id,dossier_id,lock=True)
                    await con.execute(
                        """UPDATE aios.character_research_materialization
                           SET status='failed',updated_at=now()
                           WHERE selection_id=$1 AND status='pending'""",
                        selection["selection_id"])
                    await con.execute(
                        """UPDATE aios.character_research_selection
                           SET status='failed',result=$2::jsonb,completed_at=now()
                           WHERE selection_id=$1 AND status='pending'""",
                        selection["selection_id"],json.dumps({"error":str(exc)[:900]}))
            raise
