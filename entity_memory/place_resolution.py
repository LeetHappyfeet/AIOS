"""Resolve place references from the visible, ordered source turns of one branch."""
from __future__ import annotations

from collections import defaultdict

from aios_app.entity_memory.catalog import membership_status, source_graph
from aios_app.entity_memory.extraction import normalized

_PREFIX = "urn:aios:memory-entity:"
_PARENT_RELATIONS = {"located_in", "located_inside", "part_of"}


def resolve_places(payloads):
    """Return per-source aliases and their evidence without changing source IDs.

    The first usable anchor supplies the stable ID. A bare definite name needs
    an explicit parent or identification. A provisional place can gain a
    parent only while it is the sole compatible reference in the most recent
    place-bearing source. A different explicit parent starts a different place.
    """
    known = defaultdict(dict)
    bindings, receipts = {}, []
    last_place_section = None
    for payload in payloads:
        section = payload["section_id"]
        places = {a["entity_id"]: a for a in payload.get("anchors", [])
                  if a.get("kind") == "place" and a.get("place_usable") and a.get("place_head")}
        parents, identities = defaultdict(dict), defaultdict(dict)
        for index, relation in enumerate(payload.get("relations", [])):
            child, parent = relation["subject"], relation["object"]
            if child not in places or parent not in places or membership_status(relation) != "reported":
                continue
            predicate = relation["predicate"]
            if predicate in _PARENT_RELATIONS:
                parents[child][parent] = index
            elif predicate == "identifies_place":
                identities[child][parent] = index

        # A qualified mention can supply the identity of a definite mention in
        # the same turn. Conflicting declarations stay separate evidence.
        identified_by = defaultdict(list)
        for child, targets in identities.items():
            if len(targets) == 1 and places[child]["place_reference"] == "definite":
                target = next(iter(targets))
                identified_by[target].append(child)

        resolved, visiting = {}, set()
        graph = source_graph(section)

        def bind(place_id):
            if place_id in resolved:
                return resolved[place_id]
            if place_id in visiting:
                return None
            visiting.add(place_id)
            anchor = places[place_id]
            sources = identified_by.get(place_id, [])
            if len(sources) == 1:
                child = sources[0]
                canonical = bind(child) or place_id
                evidence = identities[child][place_id]
            else:
                parent_id, evidence = None, None
                if len(parents[place_id]) == 1:
                    parent, evidence = next(iter(parents[place_id].items()))
                    parent_id = bind(parent)
                identity, target = None, None
                if len(identities[place_id]) == 1:
                    target, identity_evidence = next(iter(identities[place_id].items()))
                    if identified_by.get(target) == [place_id]:
                        identity = places[target]
                        evidence = identity_evidence
                described = identity or anchor
                form = described["place_reference"]
                qualifier = described.get("place_qualifier")
                head = anchor["place_head"]
                name = normalized(described["label"]) if form == "named" else None
                descriptors = set(described.get("place_descriptors", ()))
                candidates = {}
                if name or qualifier or (parent_id and form == "definite"):
                    candidates = {
                        key: item for key, item in known[head].items()
                        if not descriptors or descriptors == item["descriptors"]
                    }
                compatible = {key: item for key, item in candidates.items()
                              if not (parent_id and item["parent"] and parent_id != item["parent"])}
                if len(parents[place_id]) > 1:
                    exact = []
                elif name or qualifier:
                    exact = [key for key, item in compatible.items()
                             if (name and item["name"] == name)
                             or (qualifier and item["qualifier"] == qualifier)]
                elif parent_id and form == "definite":
                    exact = [key for key, item in compatible.items() if item["parent"] == parent_id]
                else:
                    exact = []
                canonical = exact[0] if len(exact) == 1 else place_id
                # A parent or explicit identity can refine an immediately
                # preceding generic reference, provided there is no competitor.
                if not exact and len(candidates) == 1 and (parent_id or identity) and anchor["place_reference"] == "definite":
                    key, prior = next(iter(candidates.items()))
                    if (key != parent_id and not (prior["parent"] or prior["name"] or prior["qualifier"])
                            and prior["section"] in {last_place_section, section}):
                        canonical = key
                prior = known[head].get(canonical, {})
                known[head][canonical] = {
                    "parent": parent_id or prior.get("parent"),
                    "name": name or prior.get("name"),
                    "qualifier": qualifier or prior.get("qualifier"),
                    "descriptors": descriptors or prior.get("descriptors", set()),
                    "section": section,
                }
            resolved[place_id] = canonical
            visiting.remove(place_id)
            if canonical != place_id:
                receipt = {"mention": _PREFIX + place_id, "place": _PREFIX + canonical,
                           "source": graph}
                if evidence is not None:
                    receipt["evidence"] = f"{graph}:relation:{evidence}"
                receipts.append(receipt)
            return canonical

        for place_id in places:
            bind(place_id)
        if places:
            last_place_section = section
        bindings[section] = resolved
    return bindings, receipts
