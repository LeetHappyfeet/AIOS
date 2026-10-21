"""Bounded cached-parse collection and receipt-after-write RDF projection."""
from __future__ import annotations

import hashlib
import json
import logging

from aios_app.entity_memory.extraction import (
    MAX_MENTIONS, POLICY_VERSION, candidate_spans, extract_memory, normalized,
)

logger = logging.getLogger(__name__)
_VOCAB = None


def cached_doc(payload):
    # Reconstruct annotations without loading/running en_core_web_sm. The
    # English language object supplies only the noun-chunk iterator and vocab.
    global _VOCAB
    import spacy
    from spacy.tokens import Doc
    if _VOCAB is None:
        _VOCAB = spacy.blank("en").vocab
    return Doc(_VOCAB).from_json(json.loads(payload) if isinstance(payload, str) else payload)


def as_json(value):
    return json.loads(value) if isinstance(value, str) else value


def identity_lookup_names(doc):
    spans = candidate_spans(doc)[:MAX_MENTIONS]
    names = sorted({normalized(s.text) for s in spans} |
                   {normalized(s.root.text) for s in spans if s.root.pos_ == "PROPN"})
    return names


async def identity_matches(db, names):
    if not names:
        return {}
    rows = await db.fetch(
        """SELECT character_id,lower(btrim(value)) name FROM (
            SELECT character_id,character_id value FROM aios.character_identity
            UNION ALL SELECT character_id,display_name FROM aios.character_identity
            UNION ALL SELECT character_id,canonical_name FROM aios.character_identity
            UNION ALL SELECT character_id,alias FROM aios.character_alias
        ) names WHERE lower(btrim(value))=ANY($1::text[])""", names)
    out = {}
    for row in rows:
        out.setdefault(row["name"], set()).add(row["character_id"])
    return out


async def collect_entity_memory_once(db, *, limit=4):
    """Backfill/refresh at most limit cached sections. No parsing or ANN calls."""
    count = 0
    for _ in range(max(1, min(int(limit), 16))):
        section_id = None
        try:
            async with db.atomic():
                work = await db.fetchrow(
                    """SELECT section_id,revision,built_revision FROM aios.entity_memory_projection
                       WHERE built_revision < revision AND retry_after<=now()
                       ORDER BY retry_after,section_id FOR UPDATE SKIP LOCKED LIMIT 1""")
                if not work:
                    break
                section_id = work["section_id"]
                source = await db.fetchrow(
                    """SELECT lp.doc_json,lp.content_sha256,lp.parser_name,lp.parser_version,
                              ds.content,n.node_id,n.timeline_id,n.character_id,n.kind::text node_kind,
                              t.world_id,ie.superseded_at,n.speaker_id,n.recipient_id
                       FROM aios.linguistic_projection lp
                       JOIN aios.document_section ds ON ds.section_id=lp.section_id
                       JOIN aios.dag_node n ON n.node_id=ds.node_id
                       JOIN aios.ingest_event ie ON ie.event_id=n.event_id
                       LEFT JOIN aios.timeline t ON t.timeline_id=n.timeline_id
                       WHERE lp.section_id=$1""", section_id)
                if not source or source["superseded_at"] is not None:
                    payload, dataset, names = {}, None, []
                else:
                    contexts = await db.fetch(
                        """SELECT ccr.origin_character_id,ccr.epistemic_scope,ccr.acquisition_mode,
                                  ccr.character_instance_id,ccr.viewpoint_id,ccr.source_kind
                           FROM aios.extracted_sentence es
                           JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
                           LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
                           WHERE es.section_id=$1 ORDER BY es.sentence_index,cc.claim_id""", section_id)
                    # Missing contexts must not default to public or borrow the
                    # identity of a different claim. Context triggers wake retries.
                    if (not contexts or any(c["epistemic_scope"] is None for c in contexts)
                            or source["parser_name"] != "spacy"
                            or source["parser_version"] != "en_core_web_sm"
                            or source["content_sha256"] != hashlib.sha256(source["content"].encode()).hexdigest()):
                        if work["built_revision"]:
                            # Lost provenance must withdraw the old graph instead
                            # of leaving it readable while waiting for repair.
                            await db.execute("""UPDATE aios.entity_memory_projection
                                SET payload='{}'::jsonb,dataset=NULL,built_revision=revision,
                                    updated_at=now() WHERE section_id=$1""", section_id)
                            count += 1
                        else:
                            await db.execute("""UPDATE aios.entity_memory_projection
                                SET retry_after=now()+interval '30 seconds' WHERE section_id=$1""", section_id)
                        continue
                    owners = {c["origin_character_id"] for c in contexts if c["origin_character_id"]}
                    # Mixed ownership is kept private under a source-local scope;
                    # never choose an arbitrary owner or publish its raw mentions.
                    owner = next(iter(owners)) if len(owners) == 1 else None
                    public = not owners and not source["character_id"] and all(c["epistemic_scope"] == "world" for c in contexts)
                    dataset = "world" if public else "char"
                    scope = json.dumps([owner or source["character_id"], str(source["world_id"]),
                                        str(source["timeline_id"]), None if owner or public else str(section_id)])
                    doc = cached_doc(source["doc_json"])
                    chat = source["node_kind"] == "chat_message" and all(
                        c["acquisition_mode"] in {"character_utterance", "controller_utterance", "chat_observation"}
                        for c in contexts)
                    names = identity_lookup_names(doc) if chat else []
                    identities = await identity_matches(db, names) if chat else {}
                    participants = {normalized(v): v for v in (source["speaker_id"], source["recipient_id"]) if v} if chat else {}
                    payload = extract_memory(doc, scope=scope, section_id=str(section_id),
                                             identities=identities, participants=participants)
                    payload.update(scope=scope, owner=owner, world_id=str(source["world_id"]) if source["world_id"] else None,
                                   timeline_id=str(source["timeline_id"]), node_id=str(source["node_id"]),
                                   section_id=str(section_id), contexts=[{k: str(v) if v is not None else None
                                                                         for k,v in dict(c).items()} for c in contexts])
                await db.execute(
                    """UPDATE aios.entity_memory_projection SET
                       rdf_revision=CASE WHEN built_revision>0 AND rdf_revision=built_revision
                           AND payload=$2::jsonb AND dataset IS NOT DISTINCT FROM $3::text
                           THEN $4 ELSE rdf_revision END,
                       payload=$2::jsonb,dataset=$3,
                       built_revision=$4,policy_version=$5,identity_names=$6::text[],updated_at=now()
                       WHERE section_id=$1""",
                    section_id, json.dumps(payload), dataset, work["revision"], POLICY_VERSION, names)
                count += 1
        except Exception:
            if section_id is None:
                raise
            logger.exception("Entity memory deferred section=%s", section_id)
            await db.execute("""UPDATE aios.entity_memory_projection
                SET retry_after=now()+interval '60 seconds'
                WHERE section_id=$1 AND revision=$2""", section_id, work["revision"])
    return count


def graph_iri(section_id):
    return "urn:aios:entity-memory:section:" + str(section_id)


def rdf_update(section_id, payload):
    """Replace just this source's derived graph; all strings are RDF literals."""
    graph = graph_iri(section_id)
    triples = []
    def literal(value):
        return json.dumps(str(value), ensure_ascii=False)
    def emit(subject, predicate, value, *, iri=False):
        obj = f"<{value}>" if iri else literal(value)
        triples.append(f"<{subject}> <urn:aios:entity-memory#{predicate}> {obj} .")
    if payload:
        source = graph + ":source"
        emit(source, "type", "EntityMemorySource")
        emit(source, "authority", "evidence_only")
        emit(source, "policyVersion", payload["policy_version"])
        emit(source, "scope", payload["scope"])
        for field in ("owner", "world_id", "timeline_id", "node_id", "section_id"):
            if payload.get(field):
                emit(source, field, payload[field])
        emit(source, "contexts", json.dumps(payload["contexts"], sort_keys=True))
        emit(source, "truncated", payload["truncated"])
        for item in payload["anchors"]:
            entity = "urn:aios:memory-entity:" + item["entity_id"]
            emit(source, "hasAnchor", entity, iri=True)
            for field in ("kind", "label", "status", "character_id", "participant_id"):
                if item.get(field):
                    emit(entity, field, item[field])
        for item in payload["mentions"]:
            mention = graph + ":mention:" + str(item["start"]) + ":" + str(item["end"])
            emit(source, "hasMention", mention, iri=True)
            for field in ("text", "start", "end", "head", "ner_label", "status", "reason"):
                if item.get(field) is not None:
                    emit(mention, field, item[field])
            if item["entity_id"]:
                emit(mention, "refersTo", "urn:aios:memory-entity:" + item["entity_id"], iri=True)
        for index, item in enumerate(payload["relations"]):
            relation = graph + ":relation:" + str(index)
            emit(source, "hasRelationEvidence", relation, iri=True)
            emit(relation, "subject", "urn:aios:memory-entity:" + item["subject"], iri=True)
            emit(relation, "object", "urn:aios:memory-entity:" + item["object"], iri=True)
            for field in ("predicate", "predicate_offset", "polarity", "scope", "source_text", "status"):
                emit(relation, field, item[field])
            emit(relation, "modal", json.dumps(item["modal"]))
            emit(relation, "attribution", json.dumps(item["attribution"]))
            emit(relation, "tense", json.dumps(item["tense"]))
            emit(relation, "aspect", json.dumps(item["aspect"]))
    return (f"DELETE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}\n"
            f"INSERT {{ GRAPH <{graph}> {{\n" + "\n".join(triples) + "\n} }\n"
            f"WHERE {{ OPTIONAL {{ GRAPH <{graph}> {{ ?s ?p ?o }} }} }}")


async def project_entity_memory_once(db, fuseki, *, limit=1):
    count = 0
    for _ in range(max(1, min(int(limit), 4))):
        # Serialize writers through ACK so an old revision cannot overwrite a
        # newer graph. A failed external write rolls back the SQL receipt.
        async with db.atomic():
            row = await db.fetchrow(
                """SELECT * FROM aios.entity_memory_projection
                   WHERE rdf_revision < built_revision AND built_revision=revision
                   ORDER BY updated_at,section_id FOR UPDATE SKIP LOCKED LIMIT 1""")
            if not row:
                break
            dataset = row["dataset"]
            # The last write may have reached Fuseki before a lost SQL ACK.
            # Check both deterministic destinations, even with no receipt.
            # Clear the non-target first; tombstones clear both datasets.
            for destination in ("world", "char"):
                if destination != dataset:
                    fuseki.update(destination, rdf_update(row["section_id"], {}))
            if dataset:
                fuseki.update(dataset, rdf_update(row["section_id"], as_json(row["payload"])))
            await db.execute(
                """UPDATE aios.entity_memory_projection SET rdf_revision=built_revision,
                   projected_dataset=dataset WHERE section_id=$1""", row["section_id"])
            count += 1
    return count
