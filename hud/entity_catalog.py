"""Bounded presentation of authorized catalog evidence, never a live scene map."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re

from aios_app.entity_memory.catalog import PLACE_TYPE_EVIDENCE, SPATIAL, world_entity_iri
from aios_app.entity_memory.catalog_service import EntityCatalogService
from aios_app.entity_memory.retrieval import terms_for

log = logging.getLogger(__name__)
TITLE = "KNOWN PLACES & ENTITIES (EVIDENCE; NOT A LIVE MAP):"
NOTICE = "Reports and references do not establish current presence, ownership, or availability. Coverage is partial."
MODES = {
    "source_mention": "mentioned",
    "encounter_record": "encounter recorded",
    "relationship_record": "relationship recorded",
    "corpus_reference_exposure": "corpus exposure",
    "acquired_corpus_reference": "acquired reference",
}
CORPUS_MODES = {"corpus_reference_exposure", "acquired_corpus_reference"}
CORPUS_SOURCE_PREFIX = "urn:aios:corpus:section:"
GROUNDED_SPATIAL_RELATIONS = {SPATIAL[predicate] for predicate in PLACE_TYPE_EVIDENCE}


def _text(value, limit=64):
    return " ".join(str(value or "").split())[:limit]


def _ref(value):
    return "@" + hashlib.sha256(str(value).encode()).hexdigest()[:10]


def present_catalog(catalog, *, token_cap=280, focus_text="", location_id=None):
    """Stable references and whole evidence rows share a strict character budget.

    Full identifiers/provenance stay in the frame, while short references disambiguate
    prompt labels. No label-based deduplication or cross-source identity matching.
    """
    terms = set(terms_for(focus_text))
    familiarity = {}
    for entry in catalog.get("familiarity", []):
        familiarity.setdefault(entry["entity_id"], []).append(entry)
    candidates = []

    def add(entity, text, sources, *, related=(), evidence=None):
        label = _text(entity.get("label"))
        words = set(re.findall(r"[\w-]+", text.casefold()))
        priority = 10 * len(words & terms)
        if location_id and entity["id"] == world_entity_iri(location_id):
            priority += 100
        candidates.append(
            {
                "text": text,
                "entity_id": entity["id"],
                "entity": entity,
                "label": label,
                "sources": list(dict.fromkeys(sources)),
                "related_ids": list(related),
                "evidence": evidence,
                "priority": priority,
            }
        )

    for group in catalog.get("places", []):
        place = group["place"]
        prefix = f"Place {_text(place.get('label'))} {_ref(place['id'])}"
        edges = group.get("contents", []) + group.get("other_evidence", [])
        eligible_edge = False
        for edge in edges:
            member = edge["entity"]
            status = edge["status"]
            source = edge["source"]
            source_modes = {
                entry["mode"] for entry in familiarity.get(place["id"], [])
                if entry["source"] == source
            }
            if source.startswith(CORPUS_SOURCE_PREFIX):
                labels = f"{place.get('label', '')} {member.get('label', '')}"
                label_words = set(re.findall(r"[\w-]+", labels.casefold()))
                if not (
                    "acquired_corpus_reference" in source_modes
                    and status == "reported"
                    and edge["relation"] in GROUNDED_SPATIAL_RELATIONS
                    and terms & label_words
                ):
                    continue
            eligible_edge = True
            # Denials/conditions/quotes must remain visible in the same row.
            qualifier = ""
            if status in {"qualified", "denied", "illustrative"}:
                qualifier = f"; evidence: {_text(edge.get('source_text'), 100)}"
            modes = sorted({MODES.get(mode, "reference") for mode in source_modes})
            tags = "; ".join([status, *modes, _ref(source)])
            text = (
                f"{prefix}: {_text(member.get('label'))} {_ref(member['id'])} "
                f"— {_text(edge['relation'], 24)} [{tags}{qualifier}]"
            )
            add(place, text, [source], related=[member["id"]], evidence=edge)
        if not eligible_edge:
            entries = [entry for entry in familiarity.get(place["id"], [])
                       if entry["mode"] not in CORPUS_MODES]
            for entry in entries[:1]:
                add(
                    place,
                    f"{prefix} [{MODES.get(entry['mode'], 'reference')}; {_ref(entry['source'])}]",
                    [entry["source"]],
                )

    # People and unlocated objects remain references; never implied co-location.
    seen = set()
    for entity in catalog.get("people", []) + catalog.get("unplaced", []):
        if entity["id"] in seen:
            continue
        seen.add(entity["id"])
        entries = [entry for entry in familiarity.get(entity["id"], [])
                   if entry["mode"] not in CORPUS_MODES]
        for entry in entries[:2]:
            kind = "Person" if entity in catalog.get("people", []) else "Entity"
            add(
                entity,
                f"{kind} {_text(entity.get('label'))} {_ref(entity['id'])} "
                f"[{MODES.get(entry['mode'], 'reference')}; {_ref(entry['source'])}; current location unverified]",
                [entry["source"]],
            )

    # Keep input order for ties (the service supplies recent sections first).
    candidates.sort(key=lambda row: (
        row["sources"][0].startswith(CORPUS_SOURCE_PREFIX), -row["priority"]
    ))
    selected, seen_rows, corpus_sources = [], set(), set()
    remaining = max(0, int(token_cap) * 4 - len(TITLE) - len(NOTICE) - 4)
    for row in candidates:
        key = row["text"]
        if key in seen_rows:
            continue
        seen_rows.add(key)
        cost = len(key) + 3
        source = row["sources"][0]
        if cost > remaining or len(selected) >= 8 or source in corpus_sources:
            continue
        selected.append(row)
        if source.startswith(CORPUS_SOURCE_PREFIX):
            corpus_sources.add(source)
        remaining -= cost
    return {
        "status": "ready",
        "graph": catalog.get("graph"),
        "coordinates": catalog.get("coordinates", {}),
        "complete": False,
        "truncated": bool(catalog.get("truncated")) or len(selected) < len(seen_rows),
        "entries": selected,
    }


async def hud_catalog(db, context, *, token_cap, focus_text=""):
    if token_cap <= 0:
        return None
    try:
        catalog = await asyncio.wait_for(
            EntityCatalogService(db).character(context.instance_id, context=context),
            timeout=0.5,
        )
        return present_catalog(
            catalog,
            token_cap=token_cap,
            focus_text=focus_text,
            location_id=context.location_entity_id,
        )
    except asyncio.TimeoutError:
        log.info("HUD entity catalog deadline exceeded; continuing without optional catalog")
        return {"status": "unavailable", "reason": "timeout", "complete": False, "entries": []}
    except Exception:
        # Optional evidence must not prevent a HUD; absence never means empty scene.
        log.warning("HUD entity catalog unavailable", exc_info=True)
        return {"status": "unavailable", "complete": False, "entries": []}
