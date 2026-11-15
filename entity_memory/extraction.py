"""Walk an existing annotated Doc. No NLP inference, LLM calls or world writes."""
from __future__ import annotations

import json
import re
from uuid import UUID, uuid5

from aios_app.epistemic.epistemic_scope import classify_scope

POLICY_VERSION = "entity-memory-v3-place-evidence"
_ID_NAMESPACE = UUID("31e62fe1-2340-4e93-a313-94fe4c5d9755")
MAX_MENTIONS = 256
# Conservative exclusions, not an attempted dictionary of every concrete noun.
_ABSTRACT = set("moment time distance connection idea surprise loneliness anger happiness sadness information knowledge plan goal thought feeling way reason thing something anything nothing place".split())
_BODY = set("body shoulder arm leg hand foot head face eye ear mouth hair finger chest heart".split())
_LOCATIONS = set("kitchen room apartment house home counter beach park store school city town street road building office garden bathroom bedroom hallway".split())
_ROOMS = {"bedroom", "kitchen", "bathroom", "hallway", "room"}
_BUILDINGS = {"house", "home", "apartment", "building"}
_HEDGES = {"maybe", "perhaps", "possibly", "probably", "apparently"}
_ATTRIBUTION = {"say", "tell", "report", "claim", "think", "believe", "imagine", "remember", "suppose", "dream"}
_MODALS = {"may", "might", "could", "would", "should", "must", "will", "can"}
_QUOTE_RE = re.compile(r'"[^"]*"|“[^”]*”')
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
    mentions, anchors, relations, destinations = [], {}, [], []
    by_head = {}
    by_root = {}
    spans = candidate_spans(doc)
    quote_spans = [match.span() for match in _QUOTE_RE.finditer(doc.text)]

    def qualifiers(token):
        return sorted({child.lower_ for child in token.children
                       if (child.dep_ == "aux" and child.lower_ in _MODALS)
                       or (child.dep_ == "advmod" and child.lower_ in _HEDGES)})

    def quoted(token):
        return any(start < token.idx < end for start, end in quote_spans)

    def context(token):
        lineage = [token, *token.ancestors]
        return list(dict.fromkeys(word for item in lineage for word in qualifiers(item)))

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
            kind = "place" if head in _LOCATIONS else _NER_TYPES.get(label, "unknown")
            status = "candidate"
            key = f"mention:{section_id}:{span.start_char}:{span.end_char}"
        descriptors = sorted(normalized(t.lemma_ or t.text) for t in span if t.dep_ in {"amod", "compound"})
        # A possessor is itself unresolved until linked; don't strip it and
        # accidentally merge Mia's mug with George's mug.
        possessive = any(t.dep_ == "poss" for t in span)
        definite = any(t.lower_ in {"the", "this", "that"} and t.dep_ == "det" for t in span)
        if kind != "place" and not known and not participant and definite and not possessive:
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
        if kind == "place" and "place_head" not in anchor:
            possessor = next((t for t in span if t.dep_ == "poss"), None)
            qualifier = None
            if possessor is not None and possessor.pos_ != "PRON":
                qualifier = normalized(doc[possessor.left_edge.i:possessor.right_edge.i + 1].text).replace("’", "'")
            name_tokens = [token for token in span if token.is_alpha and token.dep_ != "det"]
            proper = label in {"GPE", "LOC", "FAC"} and (
                head not in _LOCATIONS or (root.pos_ == "PROPN" and len(name_tokens) > 1)
            )
            reference = ("named" if proper else "qualified" if qualifier else
                         "possessive" if possessive else "definite" if definite else "indefinite")
            anchor.update(place_head=head, place_reference=reference, place_qualifier=qualifier,
                          place_descriptors=descriptors,
                          place_usable=(not quoted(root) and not context(root)
                                        and classify_scope(root.sent.text) == "asserted"
                                        and not any(v.lemma_ in _ATTRIBUTION for v in root.ancestors)
                                        and not any(t.dep_ == "neg" or
                                                    (t.lower_ == "if" and t.dep_ == "mark")
                                                    for t in root.sent)))
        mention.update(entity_id=eid, status=anchor["status"], reason="registered_identity" if known else "explicit_participant" if participant else "nominal_candidate")
        by_head.setdefault(head, []).append({"entity_id": eid, "descriptors": descriptors})
        by_root[root.i] = eid

    def add_relation(subject, predicate, obj, token, target, *, place_specific=False):
        if not subject or not obj or subject == obj:
            return
        polarity = -1 if (any(c.dep_ == "neg" for c in token.children)
                          or any(c.lower_ == "no" and c.dep_ == "det" for c in target.children)) else 1
        attribution = [{"predicate": v.lemma_ or v.text,
                        "polarity": -1 if any(c.dep_ == "neg" for c in v.children) else 1,
                        "modal": qualifiers(v)} for v in token.ancestors if v.pos_ == "VERB"
                       and (not place_specific or v.lemma_ in _ATTRIBUTION
                            or any(c.dep_ == "neg" for c in v.children))]
        if quoted(token):
            attribution.append({"predicate": "quotation"})
        scope_kind = classify_scope(token.sent.text)
        if scope_kind == "asserted" and any(t.lower_ == "if" and t.dep_ == "mark" for t in token.sent):
            scope_kind = "conditional"
        relation = {"subject": subject, "predicate": predicate, "object": obj,
                    "predicate_offset": token.i, "polarity": polarity,
                    "scope": scope_kind, "modal": context(token),
                    "tense": token.morph.get("Tense"), "aspect": token.morph.get("Aspect"),
                    "attribution": attribution, "source_text": token.sent.text,
                    "status": "evidence_only"}
        if place_specific:
            relation["place_specific"] = True
        relations.append(relation)

    # Evidence uses the syntactic head and its arguments, including prepositions
    # attached to place nouns rather than to a verb.
    for token in doc:
        source = by_root.get(token.i)
        source_place = anchors.get(source, {}).get("place_head")
        if source_place and token.pos_ in {"NOUN", "PROPN"}:
            for prep in token.children:
                if prep.dep_ != "prep" or prep.lower_ not in {"of", "in", "inside", "within"}:
                    continue
                for target in prep.children:
                    obj = by_root.get(target.i)
                    parent = anchors.get(obj, {}).get("place_head")
                    if target.dep_ != "pobj" or not parent:
                        continue
                    if prep.lower_ == "of" and not (source_place in _ROOMS and parent in _BUILDINGS):
                        continue
                    predicate = "part_of" if prep.lower_ == "of" else "located_in"
                    add_relation(source, predicate, obj, prep, target, place_specific=True)

        lemma = normalized(token.lemma_ or token.text)
        if lemma in {"enter", "arrive", "go", "walk", "move", "run", "step"} and token.pos_ == "VERB":
            targets = [child for child in token.children
                       if lemma == "enter" and child.dep_ in {"dobj", "obj"}]
            for prep in token.children:
                if prep.dep_ == "prep" and (prep.lower_ in {"to", "into"}
                                           or (lemma == "arrive" and prep.lower_ in {"at", "in"})):
                    targets.extend(child for child in prep.children if child.dep_ == "pobj")
            for target in targets:
                place_id = by_root.get(target.i)
                if anchors.get(place_id, {}).get("place_usable"):
                    destinations.append({"place_id": place_id, "action": lemma})
        if lemma not in {"own", "possess", "have", "be"} or token.pos_ not in {"VERB", "AUX"}:
            continue
        subjects = [c for c in token.children if c.dep_ == "nsubj"]
        if len(subjects) != 1 or subjects[0].i not in by_root:
            continue
        source = by_root[subjects[0].i]
        place = anchors[source].get("place_head")
        if lemma in {"own", "possess", "have"}:
            for target in token.children:
                if target.dep_ not in {"dobj", "obj"}:
                    continue
                obj = by_root.get(target.i)
                if lemma == "have" and place in _BUILDINGS and anchors.get(obj, {}).get("place_head") in _ROOMS:
                    add_relation(obj, "part_of", source, token, target, place_specific=True)
                else:
                    add_relation(source, "possesses", obj, token, target)
        else:
            for prep in token.children:
                if prep.dep_ == "prep" and prep.lower_ in {"in", "at", "inside", "on", "near", "within"}:
                    predicate = "located_" + ("in" if prep.lower_ == "within" else prep.lower_)
                    for target in prep.children:
                        if target.dep_ == "pobj":
                            add_relation(source, predicate, by_root.get(target.i), token, target)
                elif prep.dep_ == "attr" and place:
                    obj = by_root.get(prep.i)
                    other = anchors.get(obj, {})
                    if other.get("place_head") == place and other.get("place_reference") in {"qualified", "named"}:
                        add_relation(source, "identifies_place", obj, token, prep, place_specific=True)
    return {"policy_version": POLICY_VERSION, "anchors": list(anchors.values()),
            "mentions": mentions, "relations": relations, "movement_destinations": destinations, "truncated": len(spans) > MAX_MENTIONS}
