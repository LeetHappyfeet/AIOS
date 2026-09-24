from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.research import research_terms

_LOW = {"it","this","that","something","anything","someone","somebody","they","them","she","he","i","you"}
_NAME = re.compile(r"\b(?:Dr\.?\s+)?[A-Z][A-Za-z0-9_-]+(?:\s+[A-Z][A-Za-z0-9_-]+){0,2}\b")


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _clip(value: Any, n: int = 220) -> str:
    return " ".join(str(value or "").split())[:n].rstrip()


@dataclass(frozen=True)
class CognitiveSubject:
    subject_id: UUID
    canonical_key: str
    subject_type: str
    entity_keys: tuple[str, ...]
    predicate_key: str | None
    object_key: str | None
    topic_key: str | None
    question_type: str | None
    question: str | None
    display_label: str
    confidence: float
    uncertainty: float
    salience: float

    @property
    def retrieval_text(self) -> str:
        parts = [self.display_label, *self.entity_keys, self.predicate_key, self.object_key, self.topic_key]
        return " ".join(str(x) for x in parts if x)


class CognitiveSubjectBuilder:
    """Canonicalize existing epistemic structures into persistent things worth thinking about."""

    def __init__(self, db: Database):
        self.db = db

    async def build(
        self, *, instance_id: UUID, focus_text: str,
        knowledge: Sequence[Mapping[str, Any]], source_node_id: UUID | None,
    ) -> tuple[CognitiveSubject, ...]:
        candidates: list[dict[str, Any]] = []
        # Prefer structured propositions already selected by cognition.
        for item in knowledge[:40]:
            subject=_norm(item.get("subject_norm"))
            predicate=_norm(item.get("predicate_norm") or item.get("predicate_family"))
            obj=_norm(item.get("object_norm"))
            topic=_norm(item.get("topic_key"))
            if not predicate or (subject in _LOW and not topic):
                continue
            entities=tuple(x for x in (subject,obj) if x and x not in _LOW and len(x)>1)
            key=self._key("proposition",entities,predicate,obj,topic)
            label=_clip(item.get("text") or " ".join(x for x in (subject,predicate,obj) if x))
            candidates.append({
                "canonical_key":key,"subject_type":"proposition","entity_keys":entities,
                "predicate_key":predicate or None,"object_key":obj or None,"topic_key":topic or None,
                "question_type":None,"question":None,"display_label":label,
                "confidence":float(item.get("effective_confidence") or item.get("confidence") or .5),
                "uncertainty":max(0.0,1.0-float(item.get("effective_confidence") or item.get("confidence") or .5)),
                "salience":float((item.get("relevance") or {}).get("total") or 0.0),
                "source_node_id":item.get("source_node_id") or source_node_id,
                "proposition_id":item.get("proposition_id"),"evidence_kind":"proposition",
            })

        # Named entities in the current focus provide a stable fallback for new
        # information whose semantic projection has not completed yet.
        names=[]
        for match in _NAME.findall(focus_text or ""):
            name=_norm(match)
            if name not in _LOW and name not in names:
                names.append(name)
        for name in names[:4]:
            terms=research_terms(focus_text,limit=12)
            context_terms=tuple(t for t in terms if t not in set(name.split()))[:5]
            predicate="about"
            key=self._key("entity",(name,),predicate,None,None)
            label=_clip(f"{name}: {' '.join(context_terms)}" if context_terms else name)
            candidates.append({
                "canonical_key":key,"subject_type":"entity","entity_keys":(name,),
                "predicate_key":predicate,"object_key":None,"topic_key":None,
                "question_type":"identity_or_relationship",
                "question":f"What is important about {name} here?",
                "display_label":label,"confidence":.55,"uncertainty":.45,"salience":1.0,
                "source_node_id":source_node_id,"proposition_id":None,"evidence_kind":"current_focus",
            })

        # Upsert strongest unique candidates and preserve every evidence crossing.
        strongest: dict[str,dict[str,Any]]={}
        for item in candidates:
            old=strongest.get(item["canonical_key"])
            if old is None or item["salience"]>old["salience"]:
                strongest[item["canonical_key"]]=item
        subjects=[]
        for item in strongest.values():
            subjects.append(await self._persist(instance_id,item))
        subjects.sort(key=lambda s:s.salience,reverse=True)
        return tuple(subjects[:12])

    async def _persist(self, instance_id: UUID, item: Mapping[str,Any]) -> CognitiveSubject:
        row=await self.db.execute_returning_row(
            """INSERT INTO aios.character_cognitive_subject(
                   instance_id,canonical_key,subject_type,entity_keys,predicate_key,object_key,
                   topic_key,question_type,question,display_label,confidence,uncertainty,salience,meta)
               VALUES($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10,$11,$12,$13,'{}'::jsonb)
               ON CONFLICT(instance_id,canonical_key) DO UPDATE SET
                   display_label=EXCLUDED.display_label,
                   confidence=GREATEST(aios.character_cognitive_subject.confidence,EXCLUDED.confidence),
                   uncertainty=LEAST(aios.character_cognitive_subject.uncertainty,EXCLUDED.uncertainty),
                   salience=GREATEST(aios.character_cognitive_subject.salience,EXCLUDED.salience),
                   question=COALESCE(EXCLUDED.question,aios.character_cognitive_subject.question),
                   last_seen_at=now(),updated_at=now(),
                   status=CASE WHEN aios.character_cognitive_subject.status='suppressed'
                               THEN 'suppressed' ELSE 'established' END
               RETURNING *""",
            instance_id,item["canonical_key"],item["subject_type"],
            json.dumps(list(item["entity_keys"])),item.get("predicate_key"),item.get("object_key"),
            item.get("topic_key"),item.get("question_type"),item.get("question"),
            item["display_label"],item["confidence"],item["uncertainty"],item["salience"])
        await self.db.execute(
            """INSERT INTO aios.character_cognitive_subject_evidence(
                   subject_id,source_node_id,proposition_id,evidence_kind,strength,meta)
               VALUES($1,$2,$3,$4,$5,'{}'::jsonb)
               ON CONFLICT DO NOTHING""",
            row["subject_id"],item.get("source_node_id"),item.get("proposition_id"),
            item["evidence_kind"],max(.05,min(1.0,float(item["confidence"]))))
        return self._from_row(row)

    @staticmethod
    def _key(kind:str,entities:Iterable[str],predicate:str|None,obj:str|None,topic:str|None)->str:
        entity="|".join(sorted({_norm(x) for x in entities if _norm(x)}))
        return f"{kind}:{entity}:{_norm(predicate)}:{_norm(obj)}:{_norm(topic)}"[:500]

    @staticmethod
    def _from_row(row: Mapping[str,Any]) -> CognitiveSubject:
        entities=row["entity_keys"]
        if isinstance(entities,str):
            try: entities=json.loads(entities)
            except json.JSONDecodeError: entities=[]
        return CognitiveSubject(
            subject_id=row["subject_id"],canonical_key=str(row["canonical_key"]),
            subject_type=str(row["subject_type"]),entity_keys=tuple(entities or ()),
            predicate_key=row["predicate_key"],object_key=row["object_key"],topic_key=row["topic_key"],
            question_type=row["question_type"],question=row["question"],display_label=str(row["display_label"]),
            confidence=float(row["confidence"]),uncertainty=float(row["uncertainty"]),salience=float(row["salience"]))


class SubjectKnowledgeDemandResolver:
    """Choose internal recall before corpus lookup for a structured subject."""

    def resolve(self, subject: CognitiveSubject, known: Sequence[Mapping[str,Any]]) -> dict[str,Any]:
        subject_terms=set(research_terms(subject.retrieval_text,limit=32))
        matching=[]
        for item in known:
            text=" ".join(str(item.get(k) or "") for k in
                          ("subject_norm","predicate_norm","object_norm","topic_key","text"))
            terms=set(research_terms(text,limit=64))
            if subject_terms and subject_terms & terms:
                matching.append(item)
        coverage=min(1.0,len(matching)/3.0)
        if matching:
            next_source="memory" if coverage < .67 else "none"
        else:
            next_source="corpus"
        query=" ".join(dict.fromkeys([
            *subject.entity_keys,
            subject.predicate_key or "",
            subject.object_key or "",
            subject.topic_key or "",
        ])).strip()
        return {"internal_coverage":coverage,"matching":matching[:6],"next_source":next_source,
                "query":query or subject.display_label,"question":subject.question}
