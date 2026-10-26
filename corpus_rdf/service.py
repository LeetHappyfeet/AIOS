"""Bounded corpus parsing and idempotent, acknowledged reference graph writes."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import quote

from aios_app.corpus_rdf.extraction import (
    MAX_PARSE_CHARS,
    POLICY,
    build_payload,
    object_value,
    sha256,
)
from aios_app.entity_memory.service import cached_doc, payload_hash

log = logging.getLogger(__name__)
NS = "urn:aios:corpus#"
TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

_SOURCE_SQL = """SELECT cs.section_id,cs.document_id,cs.content,cs.heading,cs.meta section_meta,
 cd.title,cd.source_id,cd.source_uri,cd.language,cd.meta document_meta,
 cd.epistemic_namespace,cd.identity_binding,
 COALESCE((SELECT jsonb_agg(jsonb_build_object('key',cdd.knowledge_domain,
     'name',kd.display_name,'kind',kd.domain_kind,'parent',parent.domain_key,'enabled',kd.enabled,
     'default_namespace',kd.default_epistemic_namespace)
     ORDER BY cdd.knowledge_domain)
   FROM aios.corpus_document_domain cdd
   LEFT JOIN aios.knowledge_domain kd ON kd.domain_key=cdd.knowledge_domain
   LEFT JOIN aios.knowledge_domain parent ON parent.domain_id=kd.parent_domain_id
   WHERE cdd.document_id=cd.document_id),'[]'::jsonb) domains,
 COALESCE((SELECT jsonb_agg(scope_key ORDER BY scope_key) FROM aios.corpus_document_scope
   WHERE document_id=cd.document_id),'[]'::jsonb) scopes,
 COALESCE((SELECT jsonb_agg(jsonb_build_object('facet_type',facet_type,'facet_value',facet_value))
   FROM aios.corpus_document_facet WHERE document_id=cd.document_id),'[]'::jsonb) facets
 FROM aios.corpus_section cs JOIN aios.corpus_document cd ON cd.document_id=cs.document_id
 WHERE cs.section_id=$1"""


def graph_iri(section_id):
    return "urn:aios:corpus:section:" + str(section_id)


def entity_iri(entity_id):
    return "urn:aios:corpus:entity:" + entity_id


def _parse(text):
    from aios_app.epistemic.linguistic_projection import parse_text
    from spacy.tokens import Doc

    # Bound parser sentence length as well as total source length. Cleaned web
    # pages can contain thousands of words without sentence punctuation.
    docs = []
    start = 0
    while start < len(text):
        end = min(start + 1024, len(text))
        if end < len(text):
            boundary = max(
                text.rfind("\n", start + 512, end), text.rfind(" ", start + 512, end)
            )
            if boundary >= 0:
                end = boundary + 1
        docs.append(parse_text(text[start:end]))
        start = end
    doc = Doc.from_docs(docs, ensure_whitespace=False) if docs else parse_text("")
    if doc.text != text:
        raise ValueError("corpus parse offsets changed")
    return doc.to_json()


async def collect_corpus_references_once(db, *, limit=2, parser=None):
    """Parse each changed text once; metadata revisions reuse the stored parse."""
    count = 0
    for _ in range(max(1, min(limit, 4))):
        section_id = None
        try:
            async with db.atomic():
                work = await db.fetchrow("""SELECT * FROM aios.corpus_reference_projection
                    WHERE built_revision<revision AND retry_after<=now()
                    ORDER BY retry_after,section_id FOR UPDATE SKIP LOCKED LIMIT 1""")
                if not work:
                    break
                section_id = work["section_id"]
                row = await db.fetchrow(_SOURCE_SQL, section_id)
                parse_hash = work["parse_sha256"]
                doc_json = work["doc_json"]
                if row is None:
                    payload = {}
                    parse_hash = None
                    doc_json = None
                else:
                    row = dict(row)
                    for field in ("domains", "scopes", "facets"):
                        row[field] = object_value(row[field])
                    # The configured parser is English. Preserve metadata for
                    # other languages without silently applying English NER.
                    supported = not row["language"] or row["language"].lower().split(
                        "-"
                    )[0] in {"en", "eng", "english"}
                    text = row["content"][:MAX_PARSE_CHARS] if supported else ""
                    digest = sha256(text)
                    if parse_hash != digest or doc_json is None:
                        doc_json = (
                            await asyncio.to_thread(parser or _parse, text)
                            if supported
                            else {"text": "", "tokens": []}
                        )
                        parse_hash = digest
                    payload = build_payload(row, cached_doc(doc_json))
                    payload["parse_status"] = (
                        "ready" if supported else "unsupported_language"
                    )
                await db.execute(
                    """UPDATE aios.corpus_reference_projection SET
                    payload=$2::jsonb,parse_sha256=$3,doc_json=$4::jsonb,built_revision=revision,
                    policy_version=$5,updated_at=now(),last_error=NULL WHERE section_id=$1""",
                    section_id,
                    json.dumps(payload),
                    parse_hash,
                    json.dumps(object_value(doc_json))
                    if doc_json is not None
                    else None,
                    POLICY,
                )
                count += 1
        except Exception as exc:
            if section_id is None:
                raise
            log.exception("Corpus RDF collection deferred section=%s", section_id)
            await db.execute(
                """UPDATE aios.corpus_reference_projection SET
                last_error=$2,retry_after=now()+interval '60 seconds' WHERE section_id=$1""",
                section_id,
                type(exc).__name__,
            )
    return count


def rdf_update(section_id, payload):
    """All entity statements are source-qualified reference data, never truth."""
    graph = graph_iri(section_id)
    triples = []

    def emit(subject, predicate, value, iri=False):
        obj = "<" + value + ">" if iri else json.dumps(str(value), ensure_ascii=False)
        triples.append(f"<{subject}> <{predicate}> {obj} .")

    def prop(subject, key, value, iri=False):
        if value is not None:
            emit(subject, NS + key, value, iri)

    if payload:
        source = graph + ":source"
        document = "urn:aios:corpus:document:" + payload["document_id"]
        emit(source, TYPE, NS + "ReferenceSection", True)
        emit(document, TYPE, NS + "ReferenceDocument", True)
        prop(document, "hasSection", source, True)
        prop(source, "document", document, True)
        prop(source, "authority", "reference_only")
        prop(source, "payloadHash", payload_hash(payload))
        for key in (
            "policy_version",
            "namespace",
            "section_id",
            "document_id",
            "source_sha256",
            "reference_kind",
            "identity_binding",
            "heading",
            "parsed_chars",
            "truncated",
            "parse_status",
        ):
            prop(source, key, payload.get(key))
        for key in ("namespace", "title", "source_id", "source_uri"):
            prop(document, key, payload.get(key))
        for key in payload["scopes"]:
            prop(document, "scope", key)
        for domain in payload["domains"]:
            node = "urn:aios:corpus:domain:" + quote(domain["key"], safe="")
            emit(node, TYPE, NS + "Domain", True)
            prop(document, "domain", node, True)
            for key in ("key", "name", "kind", "enabled", "default_namespace"):
                prop(node, key, domain.get(key))
            if domain.get("parent"):
                prop(
                    node,
                    "parentDomain",
                    "urn:aios:corpus:domain:" + quote(domain["parent"], safe=""),
                    True,
                )
        for index, span in enumerate(payload["roles"]):
            role = graph + ":context:" + str(index)
            emit(role, TYPE, NS + "SourceContext", True)
            prop(source, "context", role, True)
            for key, value in span.items():
                prop(role, key, value)
        for anchor in payload["anchors"]:
            entity = entity_iri(anchor["entity_id"])
            emit(entity, TYPE, NS + "ReferenceEntity", True)
            prop(source, "hasEntity", entity, True)
            for key in ("kind", "label", "status", "content_role"):
                prop(entity, key, anchor.get(key))
        for mention in payload["mentions"]:
            node = (
                graph + ":mention:" + str(mention["start"]) + ":" + str(mention["end"])
            )
            emit(node, TYPE, NS + "SourceMention", True)
            prop(source, "hasMention", node, True)
            for key in (
                "text",
                "start",
                "end",
                "head",
                "ner_label",
                "status",
                "content_role",
            ):
                prop(node, key, mention.get(key))
            if mention["entity_id"]:
                prop(node, "refersTo", entity_iri(mention["entity_id"]), True)
        for index, relation in enumerate(payload["relations"]):
            node = graph + ":relation:" + str(index)
            emit(node, TYPE, NS + "RelationEvidence", True)
            prop(source, "hasRelationEvidence", node, True)
            for key in ("subject", "object"):
                prop(node, key, entity_iri(relation[key]), True)
            for key in (
                "predicate",
                "predicate_offset",
                "polarity",
                "scope",
                "source_text",
                "content_role",
                "authority",
                "status",
            ):
                prop(node, key, relation.get(key))
            for key in ("modal", "attribution", "tense", "aspect"):
                prop(node, key, json.dumps(relation[key]))
        if payload.get("lexical_entry"):
            entry = payload["lexical_entry"]
            node = graph + ":sense:" + quote(entry["sense_key"], safe="")
            emit(node, TYPE, NS + "SourceSense", True)
            prop(source, "describesSense", node, True)
            prop(node, "term", entry["term"])
            prop(node, "authority", "source_definition_only")
    return (
        f"DELETE WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }};\nINSERT DATA {{ GRAPH <{graph}> {{\n"
        + "\n".join(triples)
        + "\n} }"
    )


async def project_corpus_references_once(db, fuseki, *, limit=1):
    count = 0
    for _ in range(max(1, min(limit, 4))):
        async with db.atomic():
            row = await db.fetchrow("""SELECT section_id,payload FROM aios.corpus_reference_projection
                WHERE rdf_revision<built_revision AND built_revision=revision
                ORDER BY updated_at,section_id FOR UPDATE SKIP LOCKED LIMIT 1""")
            if not row:
                break
            write = asyncio.create_task(
                asyncio.to_thread(
                    fuseki.update,
                    "world",
                    rdf_update(row["section_id"], object_value(row["payload"])),
                )
            )
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # A synchronous transport cannot be cancelled. Hold the row lock
                # until it finishes, preventing a late old graph from overwriting
                # a newer revision after this transaction has been released.
                try:
                    await write
                finally:
                    raise
            await db.execute(
                """UPDATE aios.corpus_reference_projection SET rdf_revision=built_revision
                WHERE section_id=$1""",
                row["section_id"],
            )
            count += 1
    return count


async def corpus_reference_status(db, domain_key):
    """Current SQL readiness for the domain UI; no graph contents or ACL grants."""
    row = await db.fetchrow(
        """SELECT count(*) total,
        count(*) FILTER (WHERE p.built_revision=p.revision AND p.rdf_revision=p.revision) ready,
        count(*) FILTER (WHERE p.section_id IS NULL OR p.built_revision<p.revision OR p.rdf_revision<p.revision) pending,
        count(*) FILTER (WHERE p.last_error IS NOT NULL) failed,
        count(*) FILTER (WHERE kd.default_epistemic_namespace IS NOT NULL
            AND kd.default_epistemic_namespace<>cd.epistemic_namespace) namespace_mismatch,
        count(*) FILTER (WHERE p.built_revision=p.revision AND p.payload->>'truncated'='true') truncated,
        count(*) FILTER (WHERE p.built_revision=p.revision AND p.payload->>'parse_status'='unsupported_language') unsupported_language
      FROM aios.corpus_section cs
      JOIN aios.corpus_document_domain cdd ON cdd.document_id=cs.document_id AND cdd.knowledge_domain=$1
      JOIN aios.corpus_document cd ON cd.document_id=cs.document_id
      LEFT JOIN aios.knowledge_domain kd ON kd.domain_key=cdd.knowledge_domain
      LEFT JOIN aios.corpus_reference_projection p ON p.section_id=cs.section_id""",
        domain_key,
    )
    return dict(row)
