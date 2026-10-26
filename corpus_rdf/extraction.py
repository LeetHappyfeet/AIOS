"""Local reference entities and conservative source roles, without identity lookup."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

from aios_app.entity_memory.extraction import anchor_id, extract_memory, normalized

POLICY = "corpus-reference-v1"
MAX_PARSE_CHARS = 16000
_ROLES = {"definition", "example", "quotation", "reference_description"}
_ROLE_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?(definition|meaning|examples?|usage examples?|quotation|quotes?)\s*(?::\s*|$)",
    re.IGNORECASE,
)


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def object_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def reference_kind(row):
    meta = object_value(row["document_meta"])
    declared = meta.get("reference_kind") or meta.get("source_type")
    facets = {
        f["facet_value"].casefold()
        for f in row.get("facets", [])
        if f["facet_type"] == "source_type"
    }
    host = (urlparse(row.get("source_uri") or "").hostname or "").casefold()
    if (
        declared in {"dictionary", "lexicon"}
        or facets & {"dictionary", "lexicon"}
        or host in {"urbandictionary.com", "www.urbandictionary.com"}
    ):
        return "dictionary"
    return "reference"


def role_spans(text, *, heading=None, meta=None, dictionary=False):
    """Only explicit section metadata/headings classify definitions/examples.

    Unmarked dictionary prose stays lexical_reference, never a world report.
    An Example: line changes the role until the next recognized heading.
    """
    declared = (meta or {}).get("content_role")
    role = (
        declared
        if declared in _ROLES
        else "lexical_reference"
        if dictionary
        else "reference_description"
    )
    match = _ROLE_HEADING.match(heading or "")
    if match and declared not in _ROLES:
        role = _heading_role(match.group(1))
    spans = []
    offset = 0
    begin = 0
    for line in text.splitlines(keepends=True):
        match = _ROLE_HEADING.match(line)
        if match:
            if offset > begin:
                spans.append({"start": begin, "end": offset, "role": role})
            begin = offset
            role = _heading_role(match.group(1))
        offset += len(line)
    if len(text) > begin:
        spans.append({"start": begin, "end": len(text), "role": role})
    return spans


def _heading_role(label):
    label = label.casefold()
    return (
        "definition"
        if label in {"definition", "meaning"}
        else "example"
        if "example" in label
        else "quotation"
    )


def build_payload(row, doc):
    namespace = row["epistemic_namespace"] or "reference"
    document_id = str(row["document_id"])
    section_id = str(row["section_id"])
    # Names can repeat within a document. Cross-document and runtime identity
    # merges require explicit evidence, even if labels/namespaces are identical.
    scope = json.dumps([POLICY, namespace, document_id], separators=(",", ":"))
    kind = reference_kind(row)
    spans = role_spans(
        doc.text,
        heading=row.get("heading"),
        meta=object_value(row["section_meta"]),
        dictionary=kind == "dictionary",
    )
    payload = extract_memory(doc, scope=scope, section_id=section_id)

    def context(offset):
        for index, span in enumerate(spans):
            if span["start"] <= offset < span["end"]:
                return span["role"], index
        return (
            "lexical_reference" if kind == "dictionary" else "reference_description",
            0,
        )

    remap = {}
    anchors = {}
    old = {a["entity_id"]: a for a in payload["anchors"]}
    for mention in payload["mentions"]:
        role, index = context(mention["start"])
        mention["content_role"] = role
        eid = mention["entity_id"]
        if not eid:
            continue
        anchor = old[eid]
        # Repeated proper names share a provisional document identity; common
        # nouns retain extractor-local identity. Example names stay example-local.
        proper = mention.get("ner_label") in {
            "PERSON",
            "ORG",
            "GPE",
            "LOC",
            "FAC",
        } or any(
            t.pos_ == "PROPN" for t in doc.char_span(mention["start"], mention["end"])
        )
        context_key = (
            f"{section_id}:{index}"
            if kind == "dictionary" or role in {"example", "quotation"}
            else "reference"
        )
        key = (
            f"name:{context_key}:{normalized(mention['text'])}"
            if proper
            else f"nominal:{context_key}:{eid}"
        )
        new_id = anchor_id(scope, key)
        remap[(eid, role, index)] = new_id
        mention["entity_id"] = new_id
        anchors.setdefault(
            new_id,
            {
                **anchor,
                "entity_id": new_id,
                "content_role": role,
                "status": "provisional",
                "character_id": None,
                "participant_id": None,
            },
        )
    relations = []
    for relation in payload["relations"]:
        role, index = context(relation["predicate_offset"])
        subject = remap.get((relation["subject"], role, index))
        obj = remap.get((relation["object"], role, index))
        if subject and obj:
            relations.append(
                {
                    **relation,
                    "subject": subject,
                    "object": obj,
                    "content_role": role,
                    "authority": "illustrative_only"
                    if kind == "dictionary" or role in {"example", "quotation"}
                    else "reference_report_only",
                }
            )
    payload.update(
        policy_version=POLICY,
        anchors=list(anchors.values()),
        relations=relations,
        scope=scope,
        namespace=namespace,
        document_id=document_id,
        section_id=section_id,
        reference_kind=kind,
        roles=spans,
        source_sha256=sha256(row["content"]),
        parsed_chars=len(doc.text),
        truncated=payload["truncated"] or len(doc.text) < len(row["content"]),
        identity_binding=row["identity_binding"],
        title=row.get("title"),
        heading=row.get("heading"),
        source_id=row.get("source_id"),
        source_uri=row.get("source_uri"),
        domains=row.get("domains", []),
        scopes=row.get("scopes", []),
    )
    # A term/sense exists only when explicitly supplied, not guessed from prose.
    meta = object_value(row["section_meta"])
    if (
        kind == "dictionary"
        and isinstance(meta.get("term"), str)
        and meta["term"].strip()
    ):
        payload["lexical_entry"] = {
            "term": meta["term"].strip(),
            "sense_key": str(meta.get("sense_id") or section_id),
        }
    return payload
