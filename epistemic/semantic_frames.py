from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import UUID

import spacy

from aios_app.db import Database

logger = logging.getLogger("aios.epistemic.semantic_frames")

DECOMPOSER_VERSION = "semantic-frame-v2"
REFERENT_RESOLVER_VERSION = "local-dag-referent-v2"

_NLP = None

PRONOUN_PERSON = {"he", "him", "his", "she", "her", "hers", "they", "them", "their", "theirs"}
RELATIVE_PRONOUNS = {"who", "whom", "whose", "which", "that"}
PRONOUN_NEUTRAL = {"it", "its", "this", "that", "these", "those"}

PROPOSITION_PREDICATES = {
    "know": ("know", "EPISTEMIC"),
    "think": ("think", "EPISTEMIC"),
    "believe": ("believe", "EPISTEMIC"),
    "suspect": ("suspect", "EPISTEMIC"),
    "assume": ("assume", "EPISTEMIC"),
    "remember": ("remember", "MEMORY"),
    "recall": ("remember", "MEMORY"),
    "forget": ("remember", "MEMORY"),
    "want": ("want", "GOAL"),
    "intend": ("intend", "GOAL"),
    "plan": ("plan", "GOAL"),
    "try": ("try", "GOAL"),
    "attempt": ("try", "GOAL"),
    "say": ("say", "COMMUNICATION"),
    "tell": ("tell", "COMMUNICATION"),
    "ask": ("ask", "COMMUNICATION"),
    "report": ("report", "COMMUNICATION"),
    "claim": ("claim", "COMMUNICATION"),
    "seem": ("seem", "DESCRIPTIVE"),
    "appear": ("appear", "DESCRIPTIVE"),
}

CLAUSE_DEPS = {"ROOT", "conj", "ccomp", "xcomp", "advcl", "relcl", "acl"}
OBJECT_DEPS = {"dobj", "obj", "attr", "oprd", "acomp"}
SUBJECT_DEPS = {"nsubj", "nsubjpass", "csubj", "csubjpass"}


@dataclass
class FrameDraft:
    index: int
    root_i: int
    parent_index: Optional[int]
    object_frame_index: Optional[int]
    subject: Optional[str]
    predicate_surface: Optional[str]
    predicate_canonical: Optional[str]
    object_text: Optional[str]
    polarity: int
    modality: str
    tense: Optional[str]
    aspect: Optional[str]
    frame_role: str
    discourse_mode: str
    extraction_confidence: float
    predicate_confidence: float
    canonical_text: str
    meta: dict


def _get_nlp():
    global _NLP
    if _NLP is None:
        logger.info("Loading spaCy en_core_web_sm for semantic frame decomposition")
        _NLP = spacy.load("en_core_web_sm", disable=["textcat"])
        logger.info("Semantic frame NLP model ready")
    return _NLP


def _span_text(tokens: Iterable) -> Optional[str]:
    values = sorted(set(tokens), key=lambda tok: tok.i)
    if not values:
        return None
    return " ".join(tok.text for tok in values).strip() or None


def _phrase(token) -> Optional[str]:
    if token is None:
        return None
    tokens = [t for t in token.subtree if t.dep_ not in CLAUSE_DEPS or t is token]
    return _span_text(tokens)


def _find_subject(root):
    subjects = [c for c in root.children if c.dep_ in SUBJECT_DEPS]
    if subjects:
        return subjects[0]
    if root.dep_ in {"conj", "xcomp", "ccomp", "advcl"}:
        head = root.head
        while head is not None and head is not head.head:
            inherited = [c for c in head.children if c.dep_ in SUBJECT_DEPS]
            if inherited:
                return inherited[0]
            head = head.head
    return None


def _find_object(root):
    direct = [c for c in root.children if c.dep_ in OBJECT_DEPS]
    if direct:
        return direct[0]
    preps = [c for c in root.children if c.dep_ == "prep"]
    for prep in preps:
        pobj = next((c for c in prep.children if c.dep_ == "pobj"), None)
        if pobj is not None:
            return pobj
    return None


def _negated(root) -> bool:
    if any(c.dep_ == "neg" for c in root.children):
        return True
    # "have no idea" and similar determiner-negated complements.
    for child in root.children:
        if child.dep_ in OBJECT_DEPS:
            if any(t.lower_ in {"no", "none", "nothing"} for t in child.subtree):
                return True
    return False


def _modality(root) -> str:
    aux = {c.lemma_.lower() for c in root.children if c.dep_ in {"aux", "auxpass"}}
    if aux & {"might", "may", "could"}:
        return "possible"
    if aux & {"should", "ought"}:
        return "expected"
    if aux & {"must"}:
        return "necessary"
    if any(c.lower_ in {"probably", "likely"} for c in root.children):
        return "probable"
    return "asserted"


def _canonical_predicate(root, object_token) -> tuple[str, float, Optional[str]]:
    lemma = root.lemma_.lower()
    object_words = {t.lemma_.lower() for t in object_token.subtree} if object_token is not None else set()

    # Construction-level semantics outrank the surface verb.
    if lemma == "have" and "idea" in object_words:
        return "know", 0.96, "have_no_idea"
    if lemma in {"be"} and object_token is not None:
        return "be_definition_of", 0.82, "copular"
    if lemma in PROPOSITION_PREDICATES:
        return PROPOSITION_PREDICATES[lemma][0], 0.93, "proposition_taking"
    return lemma, 0.82 if lemma else 0.4, None


def _discourse_mode(doc, root, canonical_predicate: Optional[str]) -> str:
    text = root.sent.text.strip()
    predicate = (canonical_predicate or root.lemma_ or "").lower()
    if '"' in text or "“" in text or "”" in text:
        if predicate in {"say", "tell", "ask", "reply", "report", "claim"}:
            return "character_speech"
    if predicate in {
        "think", "believe", "know", "remember", "fear", "want",
        "suspect", "assume", "intend", "plan",
    }:
        return "character_mental_state"
    return "narrated_observation"


def decompose_sentence(sentence: str) -> list[FrameDraft]:
    doc = _get_nlp()(sentence)
    def is_frame_root(tok) -> bool:
        if tok.dep_ not in CLAUSE_DEPS:
            return False
        if tok.pos_ in {"VERB", "AUX"}:
            return True
        if tok.pos_ in {"ADJ", "NOUN"}:
            # Nominal/adjectival predicates are valid only when they head a
            # genuine copular clause. Coordination alone must never create a
            # semantic predicate (e.g. "eyes and ears" -> ear()).
            has_copula = any(child.dep_ == "cop" for child in tok.children)
            has_subject = any(child.dep_ in SUBJECT_DEPS for child in tok.children)
            return has_copula and has_subject
        return False

    roots = [tok for tok in doc if is_frame_root(tok)]
    roots = sorted(dict.fromkeys(roots), key=lambda t: t.i)
    root_to_index = {tok.i: idx for idx, tok in enumerate(roots)}

    drafts: list[FrameDraft] = []
    for idx, root in enumerate(roots):
        subject_token = _find_subject(root)
        object_token = _find_object(root)

        child_clause = next(
            (
                c for c in root.children
                if c.dep_ in {"ccomp", "xcomp", "advcl", "relcl", "acl"}
                and c.i in root_to_index
            ),
            None,
        )
        if child_clause is None and object_token is not None:
            child_clause = next(
                (
                    c for c in object_token.subtree
                    if c.i != object_token.i
                    and c.dep_ in {"ccomp", "xcomp", "advcl", "relcl", "acl"}
                    and c.i in root_to_index
                ),
                None,
            )
        object_frame_index = root_to_index.get(child_clause.i) if child_clause is not None else None

        parent_index = None
        if root.head is not root and root.head.i in root_to_index:
            parent_index = root_to_index[root.head.i]

        predicate, predicate_confidence, construction = _canonical_predicate(root, object_token)
        polarity = -1 if _negated(root) else 1

        # Relative-clause pronouns have a local syntactic antecedent. Resolve
        # them before cross-sentence/DAG coreference.
        local_relative_antecedent = None
        if (
            root.dep_ == "relcl"
            and subject_token is not None
            and subject_token.lower_ in RELATIVE_PRONOUNS
        ):
            local_relative_antecedent = _phrase(root.head)
            subject = local_relative_antecedent
        else:
            subject = _phrase(subject_token)

        # Passive/reporting predicates do not make their grammatical theme the
        # believer/reporter. "X is believed to be Y" means an implicit source
        # reports/believes proposition(X is Y).
        passive_reporting = (
            predicate in {"believe", "think", "say", "report", "claim"}
            and subject_token is not None
            and subject_token.dep_ in {"nsubjpass", "csubjpass"}
            and object_frame_index is not None
        )
        if passive_reporting:
            subject = None

        # For proposition-taking frames, do not flatten the subordinate clause
        # into a literal object; preserve it as object_frame_id after insertion.
        object_text = None if object_frame_index is not None else _phrase(object_token)

        # spaCy's NER is used as a prior, never as authoritative identity.
        named_entities = [
            {"text": ent.text, "label": ent.label_}
            for ent in doc.ents
            if ent.start >= root.sent.start and ent.end <= root.sent.end
        ]

        canonical = " | ".join(
            value or "_"
            for value in (subject, predicate, object_text or (f"frame:{object_frame_index}" if object_frame_index is not None else None))
        )
        if polarity < 0:
            canonical = "NOT " + canonical

        drafts.append(
            FrameDraft(
                index=idx,
                root_i=root.i,
                parent_index=parent_index,
                object_frame_index=object_frame_index,
                subject=subject,
                predicate_surface=root.lemma_.lower() or root.text.lower(),
                predicate_canonical=predicate,
                object_text=object_text,
                polarity=polarity,
                modality=_modality(root),
                tense=str(root.morph.get("Tense")[0]) if root.morph.get("Tense") else None,
                aspect=str(root.morph.get("Aspect")[0]) if root.morph.get("Aspect") else None,
                frame_role="main" if root.dep_ == "ROOT" else root.dep_.lower(),
                discourse_mode=("reported_claim" if passive_reporting else _discourse_mode(doc, root, predicate)),
                extraction_confidence=0.92 if subject and predicate else 0.72 if predicate else 0.45,
                predicate_confidence=predicate_confidence,
                canonical_text=canonical,
                meta={
                    "root_text": root.text,
                    "root_dep": root.dep_,
                    "construction": construction,
                    "local_relative_antecedent": local_relative_antecedent,
                    "passive_reporting": passive_reporting,
                    "named_entities": named_entities,
                },
            )
        )
    return drafts


def _norm(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _guess_kind(value: Optional[str], named_entities: list[dict]) -> Optional[str]:
    clean = _norm(value)
    if not clean:
        return None
    for ent in named_entities:
        if _norm(ent.get("text")) == clean:
            label = ent.get("label")
            if label == "PERSON":
                return "PERSON"
            if label in {"GPE", "LOC", "FAC"}:
                return "LOCATION"
            if label == "ORG":
                return "ORGANIZATION"
            if label in {"DATE", "TIME"}:
                return "TIME"
    if clean in PRONOUN_PERSON:
        return "PERSON"
    if clean in PRONOUN_NEUTRAL:
        return "UNKNOWN"
    return "UNKNOWN"


async def _recent_antecedents(db: Database, claim_id: UUID, limit: int = 5) -> list[dict]:
    rows = await db.fetch(
        """
        WITH current AS (
            SELECT es.section_id, es.sentence_index, dn.timeline_id, dn.node_id, dn.created_at
            FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            JOIN aios.dag_node dn ON dn.node_id=ds.node_id
            WHERE cc.claim_id=$1
        )
        SELECT
            f.resolved_subject, f.resolved_object,
            f.subject_text, f.object_text,
            f.subject_entity_key, f.object_entity_key,
            f.subject_kind_guess, f.object_kind_guess,
            es.sentence_index, dn.node_id
        FROM current cur
        JOIN aios.dag_node dn ON dn.timeline_id=cur.timeline_id
        JOIN aios.document_section ds ON ds.node_id=dn.node_id
        JOIN aios.extracted_sentence es ON es.section_id=ds.section_id
        JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
        JOIN aios.claim_semantic_frame f ON f.claim_id=cc.claim_id
        WHERE (
            (es.section_id=cur.section_id AND es.sentence_index < cur.sentence_index)
            OR (
                dn.node_id <> cur.node_id
                AND dn.created_at <= cur.created_at
            )
        )
          AND f.decomposer_version=$2
        ORDER BY
            CASE WHEN es.section_id=cur.section_id THEN 0 ELSE 1 END,
            dn.created_at DESC,
            es.sentence_index DESC,
            f.frame_index DESC
        LIMIT $3
        """,
        claim_id,
        DECOMPOSER_VERSION,
        limit * 3,
    )
    return [dict(row) for row in rows]


def _choose_antecedent(value: Optional[str], candidates: list[dict]) -> tuple[Optional[str], Optional[str], float]:
    clean = _norm(value)
    if clean not in PRONOUN_PERSON | PRONOUN_NEUTRAL:
        return value, None, 0.90 if value else 0.0

    want_person = clean in PRONOUN_PERSON
    for row in candidates:
        pairs = [
            (row.get("resolved_subject") or row.get("subject_text"), row.get("subject_entity_key"), row.get("subject_kind_guess")),
            (row.get("resolved_object") or row.get("object_text"), row.get("object_entity_key"), row.get("object_kind_guess")),
        ]
        for text, entity_key, kind in pairs:
            if not text:
                continue
            if want_person and kind == "PERSON":
                return str(text), entity_key, 0.82
            if not want_person and kind not in {None, "PERSON"}:
                return str(text), entity_key, 0.68
    return value, None, 0.20


def _entity_key(kind: Optional[str], value: Optional[str], world_id: Optional[UUID]) -> Optional[str]:
    clean = _norm(value)
    if not clean or clean in PRONOUN_PERSON | PRONOUN_NEUTRAL:
        return None
    scope = str(world_id) if world_id else "unscoped"
    return f"{scope}:{(kind or 'UNKNOWN').lower()}:{clean}"


async def decompose_claim_frames(db: Database, *, claim_id: UUID) -> int:
    projection = await db.fetchrow(
        "SELECT decomposer_version FROM aios.claim_semantic_frame_projection WHERE claim_id=$1",
        claim_id,
    )
    if projection and projection["decomposer_version"] == DECOMPOSER_VERSION:
        return 0

    row = await db.fetchrow(
        """
        SELECT
            cc.raw_text, t.world_id,
            dn.character_id, dn.speaker_id,
            dn.speaker_role::text AS speaker_role,
            dn.recipient_id,
            COALESCE(NULLIF(dn.viewpoint_id,''), NULLIF(dn.payload->>'viewpoint_id','')) AS viewpoint_id,
            COALESCE(NULLIF(dn.payload->>'identity_ruleset',''), 'character-id-v1') AS identity_ruleset
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
        JOIN aios.document_section ds ON ds.section_id=es.section_id
        JOIN aios.dag_node dn ON dn.node_id=ds.node_id
        LEFT JOIN aios.timeline t ON t.timeline_id=dn.timeline_id
        WHERE cc.claim_id=$1
        """,
        claim_id,
    )
    if not row:
        raise RuntimeError(f"Cannot decompose missing or unlinked claim {claim_id}")

    drafts = decompose_sentence(row["raw_text"])
    if not drafts:
        drafts = [
            FrameDraft(
                index=0, root_i=0, parent_index=None, object_frame_index=None,
                subject=None, predicate_surface=None, predicate_canonical=None,
                object_text=row["raw_text"], polarity=1, modality="asserted",
                tense=None, aspect=None, frame_role="fallback",
                discourse_mode="narrated_observation",
                extraction_confidence=0.25, predicate_confidence=0.0,
                canonical_text=row["raw_text"].strip(),
                meta={"fallback": True},
            )
        ]

    await db.execute(
        "DELETE FROM aios.claim_semantic_frame WHERE claim_id=$1 AND decomposer_version=$2",
        claim_id,
        DECOMPOSER_VERSION,
    )

    inserted: dict[int, UUID] = {}
    for draft in drafts:
        named_entities = draft.meta.get("named_entities", [])
        subject_kind = _guess_kind(draft.subject, named_entities)
        object_kind = _guess_kind(draft.object_text, named_entities)
        result = await db.execute_returning_row(
            """
            INSERT INTO aios.claim_semantic_frame (
                claim_id, frame_index, subject_text, predicate_surface,
                predicate_canonical, object_text, subject_kind_guess,
                object_kind_guess, polarity, modality, tense, aspect,
                discourse_mode, frame_role, resolution_status,
                extraction_confidence, predicate_confidence, entity_confidence,
                referent_confidence, frame_confidence, canonical_text,
                decomposer_version, meta
            )
            VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                'unresolved',$15,$16,$17,0.0,$18,$19,$20,$21::jsonb
            )
            RETURNING frame_id
            """,
            claim_id, draft.index, draft.subject, draft.predicate_surface,
            draft.predicate_canonical, draft.object_text, subject_kind, object_kind,
            draft.polarity, draft.modality, draft.tense, draft.aspect,
            draft.discourse_mode, draft.frame_role, draft.extraction_confidence,
            draft.predicate_confidence,
            0.85 if subject_kind not in {None, "UNKNOWN"} or object_kind not in {None, "UNKNOWN"} else 0.45,
            min(draft.extraction_confidence, draft.predicate_confidence),
            draft.canonical_text, DECOMPOSER_VERSION, json.dumps(draft.meta),
        )
        inserted[draft.index] = result["frame_id"]

    for draft in drafts:
        parent_id = inserted.get(draft.parent_index) if draft.parent_index is not None else None
        object_frame_id = inserted.get(draft.object_frame_index) if draft.object_frame_index is not None else None
        await db.execute(
            """
            UPDATE aios.claim_semantic_frame
            SET parent_frame_id=$2, object_frame_id=$3
            WHERE frame_id=$1
            """,
            inserted[draft.index], parent_id, object_frame_id,
        )

    antecedents = await _recent_antecedents(db, claim_id)
    resolved_count = 0
    for draft in drafts:
        frame_id = inserted[draft.index]
        subject_resolved, subject_key, subject_ref_conf = _choose_antecedent(draft.subject, antecedents)
        object_resolved, object_key, object_ref_conf = _choose_antecedent(draft.object_text, antecedents)

        frame_row = await db.fetchrow(
            "SELECT subject_kind_guess, object_kind_guess, object_frame_id FROM aios.claim_semantic_frame WHERE frame_id=$1",
            frame_id,
        )
        if subject_key is None:
            subject_key = _entity_key(frame_row["subject_kind_guess"], subject_resolved, row["world_id"])
        if object_key is None:
            object_key = _entity_key(frame_row["object_kind_guess"], object_resolved, row["world_id"])

        relevant_refs = [c for c, v in ((subject_ref_conf, draft.subject), (object_ref_conf, draft.object_text)) if v]
        referent_confidence = min(relevant_refs) if relevant_refs else 0.75
        has_required = bool(draft.predicate_canonical and (subject_resolved or draft.frame_role == "fallback"))
        unresolved_pronoun = any(
            _norm(v) in PRONOUN_PERSON | PRONOUN_NEUTRAL and key is None
            for v, key in ((draft.subject, subject_key), (draft.object_text, object_key))
            if v
        )
        status = "partial" if unresolved_pronoun else "resolved" if has_required else "partial"
        if status == "resolved":
            resolved_count += 1

        frame_conf = min(
            1.0,
            0.40 * draft.extraction_confidence
            + 0.30 * draft.predicate_confidence
            + 0.30 * referent_confidence,
        )
        await db.execute(
            """
            UPDATE aios.claim_semantic_frame
            SET resolved_subject=$2, resolved_object=$3,
                subject_entity_key=$4, object_entity_key=$5,
                referent_confidence=$6, frame_confidence=$7,
                resolution_status=$8, resolver_version=$9,
                resolved_at=now()
            WHERE frame_id=$1
            """,
            frame_id, subject_resolved, object_resolved, subject_key, object_key,
            referent_confidence, frame_conf, status, REFERENT_RESOLVER_VERSION,
        )

    # Prefer the root/main frame, but require semantic content. This keeps the
    # legacy primary pointer useful while all subordinate frames remain
    # durable for proposition fan-out and Qdrant.
    primary = await db.fetchrow(
        """
        SELECT frame_id
        FROM aios.claim_semantic_frame
        WHERE claim_id=$1 AND decomposer_version=$2
        ORDER BY
            CASE WHEN frame_role='main' THEN 0 ELSE 1 END,
            frame_confidence DESC,
            frame_index
        LIMIT 1
        """,
        claim_id,
        DECOMPOSER_VERSION,
    )
    await db.execute(
        """
        INSERT INTO aios.claim_semantic_frame_projection (
            claim_id, primary_frame_id, decomposer_version, resolver_version,
            frame_count, resolved_count, projected_at, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,now(),$7::jsonb)
        ON CONFLICT (claim_id) DO UPDATE
        SET primary_frame_id=EXCLUDED.primary_frame_id,
            decomposer_version=EXCLUDED.decomposer_version,
            resolver_version=EXCLUDED.resolver_version,
            frame_count=EXCLUDED.frame_count,
            resolved_count=EXCLUDED.resolved_count,
            projected_at=now(),
            meta=EXCLUDED.meta
        """,
        claim_id,
        primary["frame_id"] if primary else None,
        DECOMPOSER_VERSION,
        REFERENT_RESOLVER_VERSION,
        len(drafts),
        resolved_count,
        json.dumps({"raw_claim_preserved": True}),
    )
    return len(drafts)
