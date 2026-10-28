"""Place-oriented reference views. Membership evidence never creates world state."""

from __future__ import annotations

import json
from uuid import UUID

NS = "urn:aios:catalog#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SPATIAL = {
    "located_in": "in",
    "located_at": "at",
    "located_inside": "inside",
    "located_on": "on",
    "located_near": "near",
}
PLACE_TYPES = {
    "place",
    "location",
    "room",
    "bedroom",
    "kitchen",
    "store",
    "building",
    "region",
}
PERSON_TYPES = {"person", "character", "agent", "user"}


PLACE_CLASSES = {
    "place": "Place",
    "location": "Place",
    "room": "Room",
    "bedroom": "Bedroom",
    "kitchen": "Kitchen",
    "store": "Store",
    "building": "Building",
    "region": "Region",
}


def place_category(entity, mentions=()):
    if entity.get("kind") in PLACE_CLASSES and entity["kind"] not in {
        "place",
        "location",
    }:
        return entity["kind"]
    heads = {
        m.get("head") for m in mentions if m.get("entity_id") == entity.get("entity_id")
    }
    categories = heads & (PLACE_CLASSES.keys() - {"place", "location"})
    return next(iter(categories)) if len(categories) == 1 else "place"


def world_entity_iri(entity_id):
    return "urn:aios:world-entity:" + str(entity_id)


def source_graph(section_id, *, corpus=False):
    return (
        "urn:aios:corpus:section:" if corpus else "urn:aios:entity-memory:section:"
    ) + str(section_id)


def membership_status(relation):
    if (
        relation.get("content_role")
        in {"example", "quotation", "definition", "lexical_reference"}
        or relation.get("authority") == "illustrative_only"
    ):
        return "illustrative"
    if relation.get("polarity", 1) != 1:
        return "denied"
    if (
        relation.get("scope") != "asserted"
        or relation.get("modal")
        or relation.get("attribution")
    ):
        return "qualified"
    return "reported"


def reference_view(payload, *, corpus=False):
    """Group existing anchors; preserve IDs and every original relationship."""
    graph = source_graph(payload["section_id"], corpus=corpus)
    prefix = "urn:aios:corpus:entity:" if corpus else "urn:aios:memory-entity:"
    entities = [
        {
            "id": prefix + a["entity_id"],
            "label": a["label"],
            "kind": a["kind"],
            "authority": "reference_only" if corpus else "source_report_only",
        }
        for a in payload.get("anchors", [])
    ]
    for anchor, entity in zip(payload.get("anchors", []), entities):
        if entity["kind"] == "place":
            entity["category"] = place_category({**anchor}, payload.get("mentions", []))
    by_id = {e["id"]: e for e in entities}
    places = {
        e["id"]: {"place": e, "contents": [], "other_evidence": []}
        for e in entities
        if e["kind"] == "place"
    }
    for index, relation in enumerate(payload.get("relations", [])):
        subject = prefix + relation["subject"]
        target = prefix + relation["object"]
        if (
            relation["predicate"] not in SPATIAL
            or subject not in by_id
            or target not in places
        ):
            continue
        status = membership_status(relation)
        edge = {
            "id": graph + ":relation:" + str(index),
            "entity": by_id[subject],
            "place_id": target,
            "relation": SPATIAL[relation["predicate"]],
            "status": status,
            "source": graph,
            "source_text": relation.get("source_text"),
            "polarity": relation.get("polarity"),
            "scope": relation.get("scope"),
            "modal": relation.get("modal", []),
            "attribution": relation.get("attribution", []),
        }
        # Nearness is useful context, but is not containment.
        key = (
            "contents"
            if status == "reported" and edge["relation"] != "near"
            else "other_evidence"
        )
        places[target][key].append(edge)
    grouped = {edge["entity"]["id"] for p in places.values() for edge in p["contents"]}
    return {
        "source": graph,
        "entities": entities,
        "places": list(places.values()),
        "people": [e for e in entities if e["kind"] == "person"],
        "unplaced": [
            e for e in entities if e["kind"] != "place" and e["id"] not in grouped
        ],
    }


def reference_triples(payload, *, corpus=False):
    """Add navigation links to existing source graphs, without naked locations."""
    view = reference_view(payload, corpus=corpus)
    triples = []

    def emit(subject, predicate, value, iri=False):
        obj = f"<{value}>" if iri else json.dumps(str(value), ensure_ascii=False)
        triples.append(f"<{subject}> <{predicate}> {obj} .")

    for entity in view["entities"]:
        kind = (
            "Place"
            if entity["kind"] == "place"
            else "PersonReference"
            if entity["kind"] == "person"
            else "EntityReference"
        )
        emit(entity["id"], RDF_TYPE, NS + kind, True)
        emit(entity["id"], RDFS_LABEL, entity["label"])
        if entity["kind"] in PLACE_TYPES:
            category = entity.get("category", entity["kind"])
            emit(entity["id"], NS + "placeKind", category)
            emit(
                entity["id"], RDF_TYPE, NS + PLACE_CLASSES.get(category, "Place"), True
            )
        emit(entity["id"], NS + "describedBy", view["source"], True)
    for place in view["places"]:
        for edge in place["contents"] + place["other_evidence"]:
            emit(place["place"]["id"], NS + "hasMembershipEvidence", edge["id"], True)
            emit(edge["id"], RDF_TYPE, NS + "MembershipEvidence", True)
            for key, value in (
                ("member", edge["entity"]["id"]),
                ("place", edge["place_id"]),
                ("source", view["source"]),
            ):
                emit(edge["id"], NS + key, value, True)
            emit(edge["id"], NS + "membershipStatus", edge["status"])
            emit(edge["id"], NS + "spatialRelation", edge["relation"])
    if not corpus:
        # Only explicit instance coordinates establish an observer. A shared
        # character name does not spread references to every instance.
        observers = set()
        contexts = payload.get("contexts", [])
        for context in contexts:
            value = context.get("character_instance_id")
            try:
                observers.add(str(UUID(value)))
            except (ValueError, TypeError, AttributeError):
                observers.clear()
                break
        # Mixed or incomplete source ownership cannot establish that all
        # entities were available to any one of its observers.
        if len(observers) != 1:
            observers.clear()
        for observer in sorted(observers):
            for index, entity in enumerate(view["entities"]):
                node = view["source"] + ":reference:" + observer + ":" + str(index)
                emit(
                    "urn:aios:character-instance:" + observer,
                    NS + "hasEntityReference",
                    node,
                    True,
                )
                emit(node, NS + "entity", entity["id"], True)
                emit(node, NS + "source", view["source"], True)
                emit(node, NS + "knowledgeMode", "source_mention")
    return triples


def catalog_trig(catalog):
    """Serialize the bounded JSON view as named-graph RDF without RDF libraries."""
    triples = []

    def emit(subject, predicate, value, iri=False):
        obj = f"<{value}>" if iri else json.dumps(str(value), ensure_ascii=False)
        triples.append(f"<{subject}> <{predicate}> {obj} .")

    for entity in catalog["entities"]:
        kind = (
            "Place"
            if entity["kind"] in PLACE_TYPES
            else "PersonReference"
            if entity["kind"] in PERSON_TYPES
            else "EntityReference"
        )
        emit(entity["id"], RDF_TYPE, NS + kind, True)
        emit(entity["id"], RDFS_LABEL, entity["label"])
        if entity["kind"] in PLACE_TYPES:
            category = entity.get("category", entity["kind"])
            emit(entity["id"], NS + "placeKind", category)
            emit(
                entity["id"], RDF_TYPE, NS + PLACE_CLASSES.get(category, "Place"), True
            )
        emit(entity["id"], NS + "authority", entity["authority"])
    for place in catalog["places"]:
        for edge in place["contents"] + place["other_evidence"]:
            emit(place["place"]["id"], NS + "hasMembershipEvidence", edge["id"], True)
            emit(edge["id"], RDF_TYPE, NS + "MembershipEvidence", True)
            emit(edge["id"], NS + "member", edge["entity"]["id"], True)
            emit(edge["id"], NS + "place", place["place"]["id"], True)
            emit(edge["id"], NS + "membershipStatus", edge["status"])
            emit(edge["id"], NS + "spatialRelation", edge["relation"])
            emit(edge["id"], NS + "source", edge["source"], True)
            for key in ("source_text", "polarity", "scope"):
                if edge.get(key) is not None:
                    emit(edge["id"], NS + key, edge[key])
            for key in ("modal", "attribution"):
                if key in edge:
                    emit(edge["id"], NS + key, json.dumps(edge[key]))
    for index, reference in enumerate(catalog.get("familiarity", [])):
        node = catalog["graph"] + ":reference:" + str(index)
        emit(catalog["observer"], NS + "hasEntityReference", node, True)
        emit(node, NS + "entity", reference["entity_id"], True)
        emit(node, NS + "knowledgeMode", reference["mode"])
        emit(node, NS + "source", reference["source"], True)
    emit(catalog["graph"], NS + "viewKind", catalog["view"])
    emit(catalog["graph"], NS + "truncated", catalog["truncated"])
    return f"<{catalog['graph']}> {{\n" + "\n".join(triples) + "\n}"
