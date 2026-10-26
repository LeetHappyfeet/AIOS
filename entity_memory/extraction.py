"""Walk an existing annotated Doc. No NLP inference, LLM calls or world writes."""
from __future__ import annotations

import json
import re
from uuid import UUID, uuid5

from aios_app.epistemic.epistemic_scope import classify_scope

POLICY_VERSION = "entity-memory-v2"
_ID_NAMESPACE = UUID("31e62fe1-2340-4e93-a313-94fe4c5d9755")
MAX_MENTIONS = 256
# Conservative exclusions, not an attempted dictionary of every concrete noun.
_ABSTRACT = set("moment time distance connection idea surprise loneliness anger happiness sadness information knowledge plan goal thought feeling way reason thing something anything nothing place".split())
_BODY = set("body shoulder arm leg hand foot head face eye ear mouth hair finger chest heart".split())
_LOCATIONS = set("kitchen room apartment house home counter beach park store school city town street road building office garden bathroom bedroom hallway".split())
_NER_TYPES = {"PERSON": "person", "ORG": "organization", "GPE": "place", "LOC": "place", "FAC": "place", "PRODUCT": "object"}


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def anchor_id(scope: str, key: str) -> str:
    # Types and labels are deliberately absent from the identity material.
    return str(uuid5(_ID_NAMESPACE, json.dumps([scope, key], ensure_ascii=True)))


def candidate_spans(doc):
    """NER catches proper names; noun chunks cover ordinary objects and places."""
    chunks = list(doc.noun_chunks) if doc.has_annotation("DEP") else []
    spans = list(chunks)
    chunk_index = 0
    for ent in doc.ents:
        while chunk_index < len(chunks) and chunks[chunk_index].end <= ent.start:
            chunk_index += 1
        contained = (chunk_index < len(chunks)
                     and chunks[chunk_index].start <= ent.start
                     and chunks[chunk_index].end >= ent.end)
        if not contained:
            spans.append(ent)
    return sorted(spans, key=lambda s: (s.start_char, s.end_char))


def extract_memory(doc, *, scope: str, section_id: str, identities=None, participants=None):
    """Return evidence mentions and source-scoped provisional entity anchors.

    identities maps exact names/aliases to sets of registered character IDs.
    Unknown names stay source-scoped; ordinary definite descriptions can link
    only to one compatible earlier mention in this same source. No proximity
    resolution of pronouns and no claim that every noun denotes a concrete item.
    """
    identities = identities or {}
    participants = participants or {}
    mentions, anchors, relations = [], {}, []
    by_head = {}
    by_root = {}
    spans = candidate_spans(doc)
    for span in spans[:MAX_MENTIONS]:
        root = span.root
        head = normalized(root.lemma_ or root.text)
        text = span.text
        name = normalized(text)
        labels = [ent.label_ for ent in span.ents]
        label = labels[0] if len(labels) == 1 else None
        matches = set(identities.get(name, ()))
        # A chunk may contain a determiner around an otherwise exact name.
        if not matches and root.pos_ == "PROPN":
            matches = set(identities.get(normalized(root.text), ()))
        mention = {"text": text, "start": span.start_char, "end": span.end_char,
                   "head": head, "ner_label": label, "entity_id": None,
                   "sentence_start": root.sent.start_char, "sentence_end": root.sent.end_char,
                   "status": "unresolved", "reason": "unsupported_nominal"}
        mentions.append(mention)
        if not any(c.isalpha() for c in text):
            mention["reason"] = "formatting"
            continue
        if root.pos_ == "PRON":
            mention["reason"] = "unresolved_pronoun"
            continue
        if len(matches) > 1:
            mention["reason"] = "ambiguous_identity"
            continue
        known = next(iter(matches), None)
        participant = participants.get(name) if not known else None
        if known:
            kind, key, status = "person", "character:" + known, "linked"
        elif participant:
            kind, key, status = "person", "participant:" + participant, "linked"
        else:
            if head in _BODY:
                mention["reason"] = "body_part"
                continue
            if head in _ABSTRACT or label in {"DATE", "TIME", "CARDINAL", "ORDINAL", "QUANTITY", "MONEY", "PERCENT", "NORP"}:
                mention["reason"] = "abstract_or_nonentity"
                continue
            if root.pos_ not in {"NOUN", "PROPN"}:
                mention["reason"] = "attribute_or_fragment"
                continue
            kind = _NER_TYPES.get(label, "place" if head in _LOCATIONS else "unknown")
            status = "candidate"
            key = f"mention:{section_id}:{span.start_char}:{span.end_char}"
        descriptors = sorted(normalized(t.lemma_ or t.text) for t in span if t.dep_ in {"amod", "compound"})
        # A possessor is itself unresolved until linked; don't strip it and
        # accidentally merge Mia's mug with George's mug.
        possessive = any(t.dep_ == "poss" for t in span)
        definite = any(t.lower_ in {"the", "this", "that"} and t.dep_ == "det" for t in span)
        if not known and not participant and definite and not possessive:
            previous = {item["entity_id"]: item for item in by_head.get(head, [])
                        if set(descriptors) <= set(item["descriptors"])}
            if len(previous) > 1:
                mention.update(status="ambiguous", reason="ambiguous_local_reference")
                continue
            if len(previous) == 1:
                eid = next(iter(previous))
            else:
                eid = anchor_id(scope, key)
        else:
            eid = anchor_id(scope, key)
        anchor = anchors.setdefault(eid, {"entity_id": eid, "kind": kind, "label": text,
                                         "character_id": known, "participant_id": participant, "status": status})
        mention.update(entity_id=eid, status=anchor["status"], reason="registered_identity" if known else "explicit_participant" if participant else "nominal_candidate")
        by_head.setdefault(head, []).append({"entity_id": eid, "descriptors": descriptors})
        by_root[root.i] = eid

    # A small vocabulary of syntactically explicit relationship mentions.
    # These are reified evidence, NEVER bare owns/locatedAt truth triples.
    for verb in doc:
        lemma = normalized(verb.lemma_ or verb.text)
        if lemma not in {"own", "possess", "have", "be"} or verb.pos_ not in {"VERB", "AUX"}:
            continue
        subjects = [c for c in verb.children if c.dep_ == "nsubj"]
        if len(subjects) != 1 or subjects[0].i not in by_root:
            continue
        source = by_root[subjects[0].i]
        targets = []
        if lemma in {"own", "possess", "have"}:
            targets = [("possesses", c) for c in verb.children if c.dep_ in {"dobj", "obj"}]
        else:
            for prep in verb.children:
                if prep.dep_ == "prep" and prep.lower_ in {"in", "at", "inside", "on", "near"}:
                    targets.extend(("located_" + prep.lower_, c) for c in prep.children if c.dep_ == "pobj")
        for predicate, target in targets:
            eid = by_root.get(target.i)
            if not eid or eid == source:
                continue
            # Qualify with local and enclosing clause context, including speech,
            # belief, modal auxiliaries and negation. Do not infer ownership
            # merely from a possessive determiner.
            def modals(token):
                return [c.lower_ for c in token.children if c.dep_ == "aux"
                        and c.lower_ in {"may", "might", "could", "would", "should", "must", "will", "can"}]
            polarity = -1 if (any(c.dep_ == "neg" for c in verb.children)
                              or any(c.lower_ == "no" and c.dep_ == "det" for c in target.children)) else 1
            attribution = [{"predicate": v.lemma_ or v.text,
                            "polarity": -1 if any(c.dep_ == "neg" for c in v.children) else 1,
                            "modal": modals(v)} for v in verb.ancestors if v.pos_ == "VERB"]
            scope_kind = classify_scope(verb.sent.text)
            if scope_kind == "asserted" and any(t.lower_ == "if" and t.dep_ == "mark" for t in verb.sent):
                scope_kind = "conditional"
            relations.append({"subject": source, "predicate": predicate, "object": eid,
                              "predicate_offset": verb.idx, "polarity": polarity,
                              "scope": scope_kind, "modal": modals(verb),
                              "tense": verb.morph.get("Tense"), "aspect": verb.morph.get("Aspect"),
                              "attribution": attribution,
                              "source_text": verb.sent.text, "status": "evidence_only"})
    return {"policy_version": POLICY_VERSION, "anchors": list(anchors.values()),
            "mentions": mentions, "relations": relations, "truncated": len(spans) > MAX_MENTIONS}
