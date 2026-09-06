from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote
from uuid import UUID

from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient

RESOLVER_VERSION = "context-resolver-v1"
DATASET = "world"
LIMINAL_GRAPH = "urn:aios:world:liminal"
RDF_RECEIPT_PREDICATE = "world:contextResolverVersion"

ENTITY_KINDS = {
    "PERSON", "LOCATION", "OBJECT", "EVENT", "MEMORY", "RELATIONSHIP",
    "BELIEF", "GOAL", "RULE", "TRAIT", "STATE", "CONCEPT",
    "ORGANIZATION", "TIME", "ACTION", "QUANTITY", "UNKNOWN",
}

PREDICATE_FAMILIES = {
    "SPATIAL", "TEMPORAL", "SOCIAL", "POSSESSION", "EPISTEMIC", "MEMORY",
    "CAUSAL", "EMOTIONAL", "IDENTITY", "DESCRIPTIVE", "RULE", "GOAL",
    "ACTION", "MEMBERSHIP", "COMMUNICATION", "UNKNOWN",
}

PIVOT_KINDS = {"PERSON", "LOCATION", "EVENT", "MEMORY", "ORGANIZATION"}

PREDICATE_GROUPS = {
    "SPATIAL": {
        "located_at", "location", "be_in", "be_at", "inside", "contains",
        "contain", "enter", "leave", "arrive", "depart",
    },
    "TEMPORAL": {
        "occurred_at", "happen_at", "before", "after", "during", "begin",
        "end", "start", "finish",
    },
    "SOCIAL": {
        "friend_of", "enemy_of", "ally_of", "parent_of", "child_of",
        "spouse_of", "sibling_of", "knows", "meet", "trust", "distrust",
    },
    "POSSESSION": {
        "own", "owns", "have", "has", "carry", "carries", "possess",
        "wear", "hold", "equip",
    },
    "EPISTEMIC": {
        "believe", "believes", "know", "knows", "think", "thinks",
        "suspect", "assume", "infer", "understand", "expect",
    },
    "MEMORY": {
        "remember", "remembers", "recall", "recalled", "forget", "forgot",
        "recognize",
    },
    "CAUSAL": {
        "cause", "causes", "caused", "because", "lead_to", "result_in",
        "prevent", "enable",
    },
    "EMOTIONAL": {
        "fear", "fears", "hate", "hates", "love", "loves", "like",
        "dislike", "want", "worry", "envy", "admire",
    },
    "IDENTITY": {
        "identity", "be", "be_definition_of", "named", "called",
        "same_as", "type_of",
    },
    "DESCRIPTIVE": {
        "color", "height", "weight", "appearance", "status", "state",
        "trait", "describe", "look", "seem",
    },
    "RULE": {
        "must", "must_not", "may", "cannot", "can_not", "required",
        "forbidden", "allowed", "prohibited",
    },
    "GOAL": {
        "goal", "intend", "intends", "plan", "plans", "seek", "seeks",
        "try", "tries", "want_to",
    },
    "MEMBERSHIP": {
        "member_of", "belongs_to", "works_for", "employed_by", "join",
        "affiliated_with",
    },
    "COMMUNICATION": {
        "say", "says", "said", "tell", "tells", "told", "ask", "reply",
        "write", "read", "report", "claim", "state",
    },
}

LOCATION_WORDS = {
    "room", "tavern", "house", "home", "city", "town", "village", "country",
    "forest", "road", "street", "dock", "station", "planet", "world", "ship",
    "building", "office", "school", "store", "bar", "kitchen", "bedroom",
}
ORGANIZATION_WORDS = {
    "guild", "company", "corporation", "army", "government", "council",
    "team", "department", "agency", "organization", "university", "church",
}
TIME_WORDS = {
    "today", "tomorrow", "yesterday", "morning", "afternoon", "evening",
    "night", "week", "month", "year", "hour", "minute",
}
EVENT_WORDS = {
    "battle", "war", "meeting", "party", "attack", "accident", "incident",
    "wedding", "funeral", "conversation", "fight", "arrival", "departure",
}


@dataclass(frozen=True)
class ClaimContext:
    claim_id: UUID
    claim_kind: str
    subject_kind: Optional[str]
    object_kind: Optional[str]
    predicate_family: str
    origin_character_id: Optional[str]
    character_instance_id: Optional[UUID]
    speaker_id: Optional[str]
    speaker_type: Optional[str]
    viewpoint_id: Optional[str]
    source_id: Optional[str]
    source_kind: Optional[str]
    target_character_id: Optional[str]
    target_world_id: Optional[UUID]
    world_id: Optional[UUID]
    timeline_id: Optional[UUID]
    dag_node_id: Optional[UUID]
    epistemic_scope: str
    acquisition_mode: Optional[str]
    subject_is_pivot: bool
    object_is_pivot: bool
    confidence: float

    def as_meta(self) -> dict:
        return {
            "resolver_version": RESOLVER_VERSION,
            "claim_kind": self.claim_kind,
            "subject_kind": self.subject_kind,
            "object_kind": self.object_kind,
            "predicate_family": self.predicate_family,
            "origin_character_id": self.origin_character_id,
            "character_instance_id": str(self.character_instance_id) if self.character_instance_id else None,
            "speaker_id": self.speaker_id,
            "speaker_type": self.speaker_type,
            "viewpoint_id": self.viewpoint_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "target_character_id": self.target_character_id,
            "target_world_id": str(self.target_world_id) if self.target_world_id else None,
            "world_id": str(self.world_id) if self.world_id else None,
            "timeline_id": str(self.timeline_id) if self.timeline_id else None,
            "dag_node_id": str(self.dag_node_id) if self.dag_node_id else None,
            "epistemic_scope": self.epistemic_scope,
            "acquisition_mode": self.acquisition_mode,
            "subject_is_pivot": self.subject_is_pivot,
            "object_is_pivot": self.object_is_pivot,
            "confidence": self.confidence,
        }


def _norm(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def classify_predicate_family(predicate: Optional[str], raw_text: str = "") -> str:
    pred = _norm(predicate).replace(" ", "_")
    text = _norm(raw_text)

    for family, predicates in PREDICATE_GROUPS.items():
        if pred in predicates:
            return family

    if re.search(r"\b(must|shall|required|forbidden|prohibited|allowed)\b", text):
        return "RULE"
    if re.search(r"\b(remember|recall|forgot|memory)\b", text):
        return "MEMORY"
    if re.search(r"\b(believe|think|suspect|assume|know)\b", text):
        return "EPISTEMIC"
    if re.search(r"\b(plan|intend|goal|trying to|wants? to)\b", text):
        return "GOAL"

    if pred:
        return "ACTION"
    return "UNKNOWN"


def classify_claim_kind(predicate_family: str, predicate: Optional[str], raw_text: str) -> str:
    if predicate_family == "MEMORY":
        return "MEMORY"
    if predicate_family == "EPISTEMIC":
        return "BELIEF"
    if predicate_family == "GOAL":
        return "GOAL"
    if predicate_family == "RULE":
        return "RULE"
    if predicate_family in {"SOCIAL", "MEMBERSHIP"}:
        return "RELATIONSHIP"
    if predicate_family in {"SPATIAL", "TEMPORAL", "DESCRIPTIVE", "EMOTIONAL", "POSSESSION"}:
        return "STATE"
    if predicate_family in {"ACTION", "CAUSAL", "COMMUNICATION"}:
        return "EVENT"
    if predicate_family == "IDENTITY":
        text = _norm(raw_text)
        if re.search(r"\b(always|usually|often|kind|brave|shy|loyal|honest|cruel|calm)\b", text):
            return "TRAIT"
        return "STATE"
    return "CONCEPT" if predicate else "UNKNOWN"


def classify_entity_kind(
    value: Optional[str],
    *,
    role: str,
    predicate_family: str,
    is_known_character: bool = False,
) -> Optional[str]:
    clean = _norm(value)
    if not clean:
        return None

    if is_known_character:
        return "PERSON"

    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:\s*[%a-zA-Z]+)?", clean):
        return "QUANTITY"

    words = set(re.findall(r"[a-z]+", clean))

    if role == "object" and predicate_family == "SPATIAL":
        return "LOCATION"
    if role == "object" and predicate_family == "TEMPORAL":
        return "TIME"
    if role == "object" and predicate_family == "MEMORY":
        return "EVENT"
    if role == "object" and predicate_family in {"EPISTEMIC", "COMMUNICATION"}:
        return "CONCEPT"
    if role == "object" and predicate_family == "MEMBERSHIP":
        return "ORGANIZATION"

    if words & LOCATION_WORDS:
        return "LOCATION"
    if words & ORGANIZATION_WORDS:
        return "ORGANIZATION"
    if words & TIME_WORDS:
        return "TIME"
    if words & EVENT_WORDS:
        return "EVENT"

    if clean in {"true", "false", "alive", "dead", "open", "closed", "missing", "unknown"}:
        return "STATE"

    # Unknown named subjects/objects remain UNKNOWN instead of being guessed
    # into PERSON/OBJECT. Later ontology/entity-linking passes can refine them.
    return "UNKNOWN"


def is_semantic_pivot(kind: Optional[str], *, role: str, predicate_family: str) -> bool:
    if not kind:
        return False
    if kind in PIVOT_KINDS:
        return True
    if kind == "OBJECT":
        return predicate_family in {"POSSESSION", "SPATIAL", "ACTION"}
    if kind == "CONCEPT":
        return predicate_family in {"EPISTEMIC", "RULE", "GOAL"}
    return False


def resolve_ingest_viewpoint(
    *,
    explicit_viewpoint_id: Optional[str],
    speaker_id: Optional[str],
    speaker_type: Optional[str],
    origin_character_id: Optional[str],
) -> Optional[str]:
    if explicit_viewpoint_id:
        return explicit_viewpoint_id
    if speaker_type == "character":
        return speaker_id or origin_character_id
    if speaker_type == "source":
        return None
    return speaker_id


def infer_acquisition_mode(*, source_kind: Optional[str], speaker_role: Optional[str], node_kind: Optional[str]) -> str:
    # DAG node kind is authoritative for the ingestion form. A chat source may
    # have a vendor name such as "SillyTavern", which must not be mistaken for
    # a document merely because the source string is not literally "chat".
    if node_kind == "chat_message":
        if speaker_role == "character":
            return "character_utterance"
        if speaker_role == "user":
            return "controller_utterance"
        return "chat_observation"
    if source_kind and source_kind not in {"chat", "unknown"}:
        return "source_document"
    return "observed_source"


async def _known_character(db: Database, value: Optional[str]) -> bool:
    clean = _norm(value)
    if not clean:
        return False
    row = await db.fetchrow(
        """
        SELECT 1
        FROM aios.character_identity ci
        LEFT JOIN aios.character_alias ca ON ca.character_id=ci.character_id
        WHERE lower(ci.character_id)=$1
           OR lower(COALESCE(ci.canonical_name,''))=$1
           OR lower(COALESCE(ci.display_name,''))=$1
           OR lower(COALESCE(ca.alias,''))=$1
        LIMIT 1
        """,
        clean,
    )
    return bool(row)


async def resolve_claim_context(
    db: Database,
    fuseki: FusekiClient,
    *,
    claim_id: UUID,
) -> ClaimContext:
    existing = await db.fetchrow(
        "SELECT * FROM aios.claim_context_resolution WHERE claim_id=$1",
        claim_id,
    )
    if existing:
        context = ClaimContext(
            claim_id=claim_id,
            claim_kind=existing["claim_kind"],
            subject_kind=existing["subject_kind"],
            object_kind=existing["object_kind"],
            predicate_family=existing["predicate_family"],
            origin_character_id=existing["origin_character_id"],
            character_instance_id=existing["character_instance_id"],
            speaker_id=existing["speaker_id"],
            speaker_type=existing["speaker_type"],
            viewpoint_id=existing["viewpoint_id"],
            source_id=existing["source_id"],
            source_kind=existing["source_kind"],
            target_character_id=existing["target_character_id"],
            target_world_id=existing["target_world_id"],
            world_id=existing["world_id"],
            timeline_id=existing["timeline_id"],
            dag_node_id=existing["dag_node_id"],
            epistemic_scope=existing["epistemic_scope"],
            acquisition_mode=existing["acquisition_mode"],
            subject_is_pivot=bool(existing["subject_is_pivot"]),
            object_is_pivot=bool(existing["object_is_pivot"]),
            confidence=float(existing["confidence"]),
        )
        receipt = await db.fetchrow(
            """
            SELECT 1
            FROM aios.rdf_promotion_log
            WHERE claim_id=$1
              AND rdf_dataset=$2
              AND rdf_graph=$3
              AND rdf_predicate=$4
            """,
            claim_id,
            DATASET,
            LIMINAL_GRAPH,
            RDF_RECEIPT_PREDICATE,
        )
        if not receipt:
            await _write_liminal_context(fuseki, context)
            await _log_rdf_context(db, context)
        return context

    row = await db.fetchrow(
        """
        SELECT
            cc.claim_id, cc.subject, cc.predicate, cc.object, cc.raw_text,
            cc.confidence, cc.extraction_rule,
            n.node_id, n.timeline_id, n.kind::text AS node_kind,
            n.character_id, n.speaker_id, n.speaker_role::text AS speaker_role,
            n.recipient_id,
            COALESCE(
                NULLIF(n.viewpoint_id,''),
                NULLIF(n.payload->>'viewpoint_id','')
            ) AS explicit_viewpoint_id,
            t.world_id,
            sd.source_type,
            ie.source AS ingest_source,
            ie.source_id,
            ie.source_kind AS explicit_source_kind,
            ie.target_character_id,
            ie.target_world_id,
            COALESCE(
                (
                    SELECT ci.instance_id
                    FROM aios.character_instance ci
                    JOIN aios.character_runtime_state rs
                      ON rs.instance_id=ci.instance_id
                    JOIN aios.timeline rt
                      ON rt.timeline_id=rs.timeline_id
                    WHERE ci.character_id=n.character_id
                      AND rs.source_timeline_id=n.timeline_id
                      AND rt.session_id IS NOT DISTINCT FROM t.session_id
                      AND rt.user_name IS NOT DISTINCT FROM t.user_name
                      AND rt.scope_key=t.scope_key
                    ORDER BY rs.updated_at DESC, ci.created_at DESC
                    LIMIT 1
                ),
                (
                    SELECT ci.instance_id
                    FROM aios.character_instance ci
                    WHERE ci.character_id=n.character_id
                      AND ci.world_id=t.world_id
                    ORDER BY
                        CASE WHEN ci.current_world_id=t.world_id THEN 0 ELSE 1 END,
                        ci.created_at
                    LIMIT 1
                )
            ) AS character_instance_id
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
        JOIN aios.document_section ds ON ds.section_id=es.section_id
        JOIN aios.dag_node n ON n.node_id=ds.node_id
        LEFT JOIN aios.timeline t ON t.timeline_id=n.timeline_id
        LEFT JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        LEFT JOIN aios.source_document sd ON sd.document_id=ds.document_id
        WHERE cc.claim_id=$1
        """,
        claim_id,
    )
    if not row:
        raise RuntimeError(f"Cannot resolve context for missing or unlinked claim {claim_id}")

    family = classify_predicate_family(row["predicate"], row["raw_text"])
    claim_kind = classify_claim_kind(family, row["predicate"], row["raw_text"])

    subject_known_character = await _known_character(db, row["subject"])
    object_known_character = await _known_character(db, row["object"])

    subject_kind = classify_entity_kind(
        row["subject"], role="subject", predicate_family=family,
        is_known_character=subject_known_character,
    )
    object_kind = classify_entity_kind(
        row["object"], role="object", predicate_family=family,
        is_known_character=object_known_character,
    )

    origin_character_id = row["character_id"]
    speaker_id = row["speaker_id"]
    speaker_type = row["speaker_role"]
    viewpoint_id = resolve_ingest_viewpoint(
        explicit_viewpoint_id=row["explicit_viewpoint_id"],
        speaker_id=speaker_id,
        speaker_type=speaker_type,
        origin_character_id=origin_character_id,
    )
    epistemic_scope = "character" if viewpoint_id == origin_character_id and origin_character_id else (
        "speaker" if viewpoint_id else "source"
    )

    source_kind = row["explicit_source_kind"] or row["source_type"] or row["ingest_source"]
    acquisition_mode = infer_acquisition_mode(
        source_kind=source_kind,
        speaker_role=row["speaker_role"],
        node_kind=row["node_kind"],
    )

    lineage_score = sum(
        value is not None
        for value in (row["node_id"], row["timeline_id"], row["world_id"])
    ) / 3.0
    semantic_score = sum(
        value not in {None, "UNKNOWN"}
        for value in (claim_kind, family, subject_kind, object_kind)
    ) / 4.0
    extraction_score = max(0.0, min(1.0, float(row["confidence"] or 0.0)))
    confidence = min(1.0, 0.50 * lineage_score + 0.35 * semantic_score + 0.15 * extraction_score)

    context = ClaimContext(
        claim_id=claim_id,
        claim_kind=claim_kind,
        subject_kind=subject_kind,
        object_kind=object_kind,
        predicate_family=family,
        origin_character_id=origin_character_id,
        character_instance_id=row["character_instance_id"],
        speaker_id=speaker_id,
        speaker_type=speaker_type,
        viewpoint_id=viewpoint_id,
        source_id=row["source_id"],
        source_kind=source_kind,
        target_character_id=row["target_character_id"],
        target_world_id=row["target_world_id"],
        world_id=row["world_id"],
        timeline_id=row["timeline_id"],
        dag_node_id=row["node_id"],
        epistemic_scope=epistemic_scope,
        acquisition_mode=acquisition_mode,
        subject_is_pivot=is_semantic_pivot(subject_kind, role="subject", predicate_family=family),
        object_is_pivot=is_semantic_pivot(object_kind, role="object", predicate_family=family),
        confidence=confidence,
    )

    await db.execute(
        """
        INSERT INTO aios.claim_context_resolution (
            claim_id, claim_kind, subject_kind, object_kind, predicate_family,
            origin_character_id, character_instance_id, speaker_id, speaker_type,
            viewpoint_id, source_id, source_kind, target_character_id,
            target_world_id, world_id, timeline_id, dag_node_id,
            epistemic_scope, acquisition_mode, subject_is_pivot,
            object_is_pivot, confidence, resolver_version, meta
        )
        VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
            $11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
            $21,$22,$23,$24::jsonb
        )
        ON CONFLICT (claim_id) DO NOTHING
        """,
        claim_id, claim_kind, subject_kind, object_kind, family,
        origin_character_id, context.character_instance_id,
        context.speaker_id, context.speaker_type, viewpoint_id,
        context.source_id, context.source_kind, context.target_character_id,
        context.target_world_id, context.world_id, context.timeline_id,
        context.dag_node_id, epistemic_scope, acquisition_mode,
        context.subject_is_pivot, context.object_is_pivot, confidence,
        RESOLVER_VERSION, json.dumps(context.as_meta()),
    )

    await _write_liminal_context(fuseki, context)
    await _log_rdf_context(db, context)
    return context


async def _log_rdf_context(db: Database, context: ClaimContext) -> None:
    claim_iri = f"urn:aios:world:claim:{context.claim_id}"
    await db.execute(
        """
        INSERT INTO aios.rdf_promotion_log (
            claim_id, rdf_dataset, rdf_graph, rdf_subject,
            rdf_predicate, rdf_object, promoted_by, promotion_meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,'context_resolver',$7::jsonb)
        ON CONFLICT (claim_id, rdf_dataset, rdf_graph, rdf_predicate) DO NOTHING
        """,
        context.claim_id,
        DATASET,
        LIMINAL_GRAPH,
        claim_iri,
        RDF_RECEIPT_PREDICATE,
        RESOLVER_VERSION,
        json.dumps({"epistemic_scope": context.epistemic_scope}),
    )


def _sparql_lit(value: Optional[str]) -> str:
    if value is None:
        return '""'
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _write_liminal_context(fuseki: FusekiClient, context: ClaimContext) -> None:
    claim_iri = f"urn:aios:world:claim:{context.claim_id}"
    triples = [
        f"<{claim_iri}> world:claimKind world:{context.claim_kind.title()} .",
        f"<{claim_iri}> world:predicateFamily world:{context.predicate_family.title()} .",
        f"<{claim_iri}> world:epistemicScope {_sparql_lit(context.epistemic_scope)} .",
        f"<{claim_iri}> world:contextResolverVersion {_sparql_lit(RESOLVER_VERSION)} .",
        f"<{claim_iri}> world:subjectIsPivot {_sparql_lit(str(context.subject_is_pivot).lower())} .",
        f"<{claim_iri}> world:objectIsPivot {_sparql_lit(str(context.object_is_pivot).lower())} .",
    ]
    if context.subject_kind:
        triples.append(f"<{claim_iri}> world:subjectKind world:{context.subject_kind.title()} .")
    if context.object_kind:
        triples.append(f"<{claim_iri}> world:objectKind world:{context.object_kind.title()} .")
    if context.origin_character_id:
        char_segment = quote(context.origin_character_id, safe="")
        triples.append(f"<{claim_iri}> world:originCharacter <urn:aios:character:{char_segment}> .")
    if context.source_id:
        triples.append(f"<{claim_iri}> world:sourceId {_sparql_lit(context.source_id)} .")
    if context.source_kind:
        triples.append(f"<{claim_iri}> world:sourceKind {_sparql_lit(context.source_kind)} .")
    if context.target_character_id:
        triples.append(f"<{claim_iri}> world:targetCharacterHint {_sparql_lit(context.target_character_id)} .")
    if context.target_world_id:
        triples.append(f"<{claim_iri}> world:targetWorldHint <urn:aios:world:{context.target_world_id}> .")
    if context.viewpoint_id:
        triples.append(f"<{claim_iri}> world:viewpointId {_sparql_lit(context.viewpoint_id)} .")
    if context.character_instance_id:
        triples.append(f"<{claim_iri}> world:originCharacterInstance <urn:aios:character-instance:{context.character_instance_id}> .")
    if context.world_id:
        triples.append(f"<{claim_iri}> world:originWorld <urn:aios:world:{context.world_id}> .")
    if context.timeline_id:
        triples.append(f"<{claim_iri}> world:originTimeline <urn:aios:timeline:{context.timeline_id}> .")
    if context.acquisition_mode:
        triples.append(f"<{claim_iri}> world:acquisitionMode {_sparql_lit(context.acquisition_mode)} .")

    sparql = f"""
PREFIX world: <urn:aios:world#>

INSERT DATA {{
  GRAPH <{LIMINAL_GRAPH}> {{
    {chr(10).join(triples)}
  }}
}}
""".strip()
    fuseki.update(DATASET, sparql)
