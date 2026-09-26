from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.research import research_terms


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _clip(value: Any, n: int = 220) -> str:
    return " ".join(str(value or "").split())[:n].rstrip()


def _uuid_or_none(value: Any) -> UUID | None:
    """Return a real UUID for database FK columns; synthetic cognition IDs stay provenance."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _confidence(item: Mapping[str, Any]) -> float:
    value = item.get("effective_confidence")
    if not isinstance(value, (int, float)):
        value = item.get("confidence")
    return max(0.0, min(1.0, float(value if isinstance(value, (int, float)) else .5)))


def _salience(item: Mapping[str, Any]) -> float:
    relevance = item.get("relevance")
    if isinstance(relevance, Mapping):
        value = relevance.get("total")
        if isinstance(value, (int, float)):
            return float(value)
    value = item.get("salience")
    return float(value) if isinstance(value, (int, float)) else 0.0


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
    """Aggregate epistemic output into persistent things worth thinking about.

    Subject identity comes from resolved entity/topic/relation structure already
    produced by message cognition and semantic projection. Raw prose is evidence
    and display context; it is never parsed here as a second NER system.
    """

    def __init__(self, db: Database):
        self.db = db

    async def build(
        self, *, instance_id: UUID, focus_text: str,
        knowledge: Sequence[Mapping[str, Any]], source_node_id: UUID | None,
    ) -> tuple[CognitiveSubject, ...]:
        del focus_text  # raw focus is deliberately not an identity source
        grouped: dict[str, dict[str, Any]] = {}

        for item in knowledge[:40]:
            candidate = self._candidate(item, source_node_id)
            if candidate is None:
                continue
            key = candidate["canonical_key"]
            group = grouped.get(key)
            if group is None:
                candidate["evidence"] = [self._evidence(item, source_node_id)]
                grouped[key] = candidate
                continue
            group["confidence"] = max(group["confidence"], candidate["confidence"])
            group["uncertainty"] = min(group["uncertainty"], candidate["uncertainty"])
            group["salience"] = max(group["salience"], candidate["salience"])
            group["entity_keys"] = tuple(sorted(set(group["entity_keys"]) | set(candidate["entity_keys"])))
            if candidate["salience"] >= group.get("_label_salience", -1.0):
                group["display_label"] = candidate["display_label"]
                group["_label_salience"] = candidate["salience"]
            group["evidence"].append(self._evidence(item, source_node_id))

        subjects: list[CognitiveSubject] = []
        for item in grouped.values():
            subjects.append(await self._persist(instance_id, item))
        subjects.sort(key=lambda value: (value.salience, value.confidence), reverse=True)
        return tuple(subjects[:12])

    def _candidate(
        self, item: Mapping[str, Any], fallback_source_node_id: UUID | None,
    ) -> dict[str, Any] | None:
        subject = _norm(item.get("resolved_subject") or item.get("subject_norm"))
        obj = _norm(item.get("resolved_object") or item.get("object_norm"))
        predicate = _norm(
            item.get("predicate_canonical")
            or item.get("predicate_norm")
            or item.get("predicate_family")
        )
        topic = _norm(item.get("topic_key"))
        subject_entity = _norm(item.get("subject_entity_key"))
        object_entity = _norm(item.get("object_entity_key"))

        # Entity keys are authoritative. Normalized proposition participants are
        # useful only when message cognition has actually resolved them.
        entities = tuple(sorted({
            value for value in (subject_entity, object_entity, subject, obj)
            if self._structured_identity(value)
        }))
        if not entities and not topic:
            return None

        kind, identity_predicate, identity_object = self._identity(
            entities=entities, predicate=predicate, obj=obj, topic=topic,
        )
        canonical_key = self._key(kind, entities, identity_predicate, topic)
        if not canonical_key:
            return None

        confidence = _confidence(item)
        salience = _salience(item)
        label = _clip(
            item.get("text")
            or " ".join(value for value in (*entities, predicate, obj, topic) if value)
        )
        question_type = None
        question = None
        if kind == "entity":
            question_type = "identity_or_relationship"
            question = f"What is important about {entities[0]} here?"
        elif kind in {"topic", "relation"}:
            question_type = "situation"
            question = f"What does this situation mean for {' and '.join(entities[:3])}?"

        return {
            "canonical_key": canonical_key,
            "subject_type": kind,
            "entity_keys": entities,
            "predicate_key": identity_predicate or None,
            "object_key": identity_object or None,
            "topic_key": topic or None,
            "question_type": question_type,
            "question": question,
            "display_label": label or topic or " / ".join(entities),
            "confidence": confidence,
            "uncertainty": 1.0 - confidence,
            "salience": salience,
            "_label_salience": salience,
            "source_node_id": item.get("source_node_id") or fallback_source_node_id,
        }

    @staticmethod
    def _structured_identity(value: str) -> bool:
        """Accept resolved identifiers, not grammatical placeholders.

        This is intentionally structural rather than a vocabulary blacklist:
        unresolved pronouns/placeholders are rejected because they do not name a
        stable participant. Ordinary words are not guessed to be entities here.
        """
        value = _norm(value)
        if not value:
            return False
        if value in {"_", "*", "other", "someone", "somebody", "something"}:
            return False
        if value in {"i", "me", "my", "we", "us", "our", "you", "your",
                     "he", "him", "his", "she", "her", "they", "them", "their", "it", "its"}:
            return False
        return True

    @staticmethod
    def _identity(
        *, entities: tuple[str, ...], predicate: str, obj: str, topic: str,
    ) -> tuple[str, str, str]:
        # Topic identity is deliberately broader than proposition identity:
        # differently worded evidence about the same participants/topic converges.
        if topic:
            return "topic", "", ""
        if len(entities) >= 2:
            return "relation", predicate, ""
        if len(entities) == 1:
            return "entity", "", ""
        return "event", predicate, obj

    @staticmethod
    def _key(kind: str, entities: tuple[str, ...], predicate: str, topic: str) -> str:
        entity = "|".join(sorted({_norm(value) for value in entities if _norm(value)}))
        if kind == "topic":
            return f"topic:{entity}:{_norm(topic)}"[:500]
        if kind == "relation":
            return f"relation:{entity}:{_norm(predicate)}"[:500]
        if kind == "entity":
            return f"entity:{entity}"[:500]
        if predicate:
            return f"event:{entity}:{_norm(predicate)}"[:500]
        return ""

    @staticmethod
    def _evidence(
        item: Mapping[str, Any], fallback_source_node_id: UUID | None,
    ) -> dict[str, Any]:
        return {
            "source_node_id": item.get("source_node_id") or fallback_source_node_id,
            "proposition_id": item.get("proposition_id"),
            "frame_id": item.get("frame_id"),
            "evidence_kind": (
                "semantic_frame" if item.get("frame_id") else
                "proposition" if item.get("proposition_id") else
                "structured_cognition"
            ),
            "confidence": _confidence(item),
        }

    async def _persist(self, instance_id: UUID, item: Mapping[str, Any]) -> CognitiveSubject:
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.character_cognitive_subject(
                   instance_id,canonical_key,subject_type,entity_keys,predicate_key,object_key,
                   topic_key,question_type,question,display_label,confidence,uncertainty,salience,meta)
               VALUES($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10,$11,$12,$13,'{}'::jsonb)
               ON CONFLICT(instance_id,canonical_key) DO UPDATE SET
                   entity_keys=EXCLUDED.entity_keys,
                   display_label=EXCLUDED.display_label,
                   confidence=GREATEST(aios.character_cognitive_subject.confidence,EXCLUDED.confidence),
                   uncertainty=LEAST(aios.character_cognitive_subject.uncertainty,EXCLUDED.uncertainty),
                   salience=GREATEST(aios.character_cognitive_subject.salience,EXCLUDED.salience),
                   question=COALESCE(EXCLUDED.question,aios.character_cognitive_subject.question),
                   last_seen_at=now(),updated_at=now(),
                   status=CASE WHEN aios.character_cognitive_subject.status='suppressed'
                               THEN 'suppressed' ELSE 'established' END
               RETURNING *""",
            instance_id, item["canonical_key"], item["subject_type"],
            json.dumps(list(item["entity_keys"])), item.get("predicate_key"), item.get("object_key"),
            item.get("topic_key"), item.get("question_type"), item.get("question"),
            item["display_label"], item["confidence"], item["uncertainty"], item["salience"],
        )

        for evidence in item.get("evidence") or ():
            source_raw = evidence.get("source_node_id")
            proposition_raw = evidence.get("proposition_id")
            frame_raw = evidence.get("frame_id")
            source_id = _uuid_or_none(source_raw)
            proposition_id = _uuid_or_none(proposition_raw)
            frame_id = _uuid_or_none(frame_raw)
            meta: dict[str, str] = {}
            if source_raw is not None and source_id is None:
                meta["source_identifier"] = str(source_raw)
            if proposition_raw is not None and proposition_id is None:
                meta["proposition_identifier"] = str(proposition_raw)
            if frame_raw is not None and frame_id is None:
                meta["frame_identifier"] = str(frame_raw)
            await self.db.execute(
                """INSERT INTO aios.character_cognitive_subject_evidence(
                       subject_id,source_node_id,proposition_id,frame_id,evidence_kind,strength,meta)
                   VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)
                   ON CONFLICT DO NOTHING""",
                row["subject_id"], source_id, proposition_id, frame_id,
                evidence["evidence_kind"],
                max(.05, min(1.0, float(evidence["confidence"]))),
                json.dumps(meta, default=str),
            )
        return self._from_row(row)

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> CognitiveSubject:
        entities = row["entity_keys"]
        if isinstance(entities, str):
            try:
                entities = json.loads(entities)
            except json.JSONDecodeError:
                entities = []
        return CognitiveSubject(
            subject_id=row["subject_id"], canonical_key=str(row["canonical_key"]),
            subject_type=str(row["subject_type"]), entity_keys=tuple(entities or ()),
            predicate_key=row["predicate_key"], object_key=row["object_key"], topic_key=row["topic_key"],
            question_type=row["question_type"], question=row["question"], display_label=str(row["display_label"]),
            confidence=float(row["confidence"]), uncertainty=float(row["uncertainty"]),
            salience=float(row["salience"]),
        )


class GoalSubjectProjector:
    """Persist an active managed goal as an explicit character cognitive subject."""

    def __init__(self, db: Database):
        self.db = db

    async def project(self, *, instance_id: UUID, goal: Any) -> CognitiveSubject | None:
        goal_id = _uuid_or_none(getattr(goal, "goal_id", None))
        text = _clip(getattr(goal, "text", ""), 220)
        if goal_id is None or not text:
            return None
        topic = _norm((getattr(goal, "meta", {}) or {}).get("semantic_topic_key"))
        key = f"goal:{goal_id}"
        terms = tuple(research_terms(topic or text, limit=12))
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.character_cognitive_subject(
                   instance_id,goal_id,canonical_key,subject_type,entity_keys,topic_key,
                   question_type,question,display_label,confidence,uncertainty,salience,status,meta)
               VALUES($1,$2,$3,'goal_knowledge',$4::jsonb,$5,'knowledge_demand',$6,$7,1.0,0.0,$8,
                      'established',$9::jsonb)
               ON CONFLICT(instance_id,canonical_key) DO UPDATE SET
                   goal_id=EXCLUDED.goal_id,entity_keys=EXCLUDED.entity_keys,
                   topic_key=COALESCE(EXCLUDED.topic_key,aios.character_cognitive_subject.topic_key),
                   question=EXCLUDED.question,display_label=EXCLUDED.display_label,
                   salience=GREATEST(aios.character_cognitive_subject.salience,EXCLUDED.salience),
                   status='established',last_seen_at=now(),updated_at=now(),
                   meta=aios.character_cognitive_subject.meta || EXCLUDED.meta
               RETURNING *""",
            instance_id, goal_id, key, json.dumps(list(terms)), topic or None,
            f"What knowledge would help satisfy this goal: {text}?", text,
            max(.25, min(1.0, (110 - int(getattr(goal, "priority", 100))) / 100.0)),
            json.dumps({"source": "managed_goal", "goal_id": str(goal_id)}),
        )
        return CognitiveSubjectBuilder._from_row(row)


class GoalKnowledgeDemandResolver:
    """Resolve a goal's knowledge demand from authoritative character topology.

    SQL semantic topology is authoritative; Fuseki /char is its derived RDF view.
    Lexical subject coverage is used only when no character topology is available.
    """

    def __init__(self, db: Database):
        self.db = db
        self.fallback = SubjectKnowledgeDemandResolver()

    async def resolve(
        self, *, instance_id: UUID, subject: CognitiveSubject,
        known: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        terms = research_terms(subject.retrieval_text, limit=24)
        rows = await self.db.fetch(
            """SELECT DISTINCT n.topology_node_id,n.node_type,n.node_key,n.label,
                              n.proposition_id,n.significance
               FROM aios.semantic_topology_node n
               WHERE n.scope_kind='character'
                 AND n.character_instance_id=$1
                 AND n.node_type IN
                     ('PROPOSITION','TOPIC','CONCEPT','SEMANTIC_PIVOT','BELIEF_STATE',
                      'EPISTEMIC_TRANSITION')
                 AND (
                     cardinality($2::text[])=0
                     OR EXISTS (
                         SELECT 1 FROM unnest($2::text[]) term
                         WHERE lower(COALESCE(n.label,'') || ' ' || COALESCE(n.node_key,''))
                               LIKE '%' || lower(term) || '%'
                     )
                 )
               ORDER BY n.significance DESC
               LIMIT 12""",
            instance_id, list(terms),
        )
        if not rows:
            result = self.fallback.resolve(subject, known)
            return {**result, "coverage_source": "lexical_fallback", "topology": []}

        propositions = {str(row["proposition_id"]) for row in rows if row["proposition_id"]}
        structural = {
            str(row["topology_node_id"]) for row in rows
            if str(row["node_type"]) not in {"EPISTEMIC_TRANSITION"}
        }
        # Three distinct proposition/structural receipts are enough to stop
        # automatic research. This is a routing threshold, not proof that the
        # managed goal itself is complete.
        evidence_units = len(propositions) + max(0, len(structural) - len(propositions))
        coverage = min(1.0, evidence_units / 3.0)
        next_source = "none" if coverage >= .67 else "memory"
        query = " ".join(dict.fromkeys(terms[:12])).strip() or subject.display_label
        return {
            "internal_coverage": coverage,
            "matching": [],
            "next_source": next_source,
            "query": query,
            "question": subject.question,
            "coverage_source": "character_topology",
            "topology": [
                {"topology_node_id": str(row["topology_node_id"]),
                 "node_type": str(row["node_type"]),
                 "proposition_id": str(row["proposition_id"]) if row["proposition_id"] else None,
                 "label": row["label"], "significance": float(row["significance"] or 0)}
                for row in rows[:6]
            ],
        }


class SubjectKnowledgeDemandResolver:
    """Choose internal recall before corpus lookup for a structured subject."""

    def resolve(self, subject: CognitiveSubject, known: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        subject_terms = set(research_terms(subject.retrieval_text, limit=32))
        matching = []
        for item in known:
            text = " ".join(str(item.get(key) or "") for key in (
                "subject_entity_key", "object_entity_key", "resolved_subject", "resolved_object",
                "subject_norm", "predicate_norm", "object_norm", "topic_key", "text",
            ))
            terms = set(research_terms(text, limit=64))
            if subject_terms and subject_terms & terms:
                matching.append(item)
        coverage = min(1.0, len(matching) / 3.0)
        next_source = "memory" if matching and coverage < .67 else "none" if matching else "corpus"
        query = " ".join(dict.fromkeys([
            *subject.entity_keys,
            subject.predicate_key or "",
            subject.topic_key or "",
        ])).strip()
        return {
            "internal_coverage": coverage, "matching": matching[:6],
            "next_source": next_source, "query": query or subject.display_label,
            "question": subject.question,
        }
