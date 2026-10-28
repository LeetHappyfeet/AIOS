"""Read-only place catalogs from current SQL coordinates and source projections."""

from __future__ import annotations

import asyncio
import hashlib

from aios_app.entity_memory.catalog import (
    PERSON_TYPES,
    PLACE_TYPES,
    reference_view,
    world_entity_iri,
)
from aios_app.entity_memory.retrieval import (
    _ANCESTRY,
    _SOURCE_ACCESS,
    _SOURCE_SELECT,
    owners_for,
    scope_key,
)
from aios_app.entity_memory.service import as_json

MAX_SOURCES = 24
MAX_EDGES = 128
READ_SECONDS = 3.0

_SOURCE_CATALOG = (
    _ANCESTRY
    + _SOURCE_SELECT
    + _SOURCE_ACCESS
    + "ORDER BY ep.updated_at DESC,ep.section_id LIMIT 25"
)
_WORLD_EDGES = (
    _ANCESTRY
    + """
SELECT r.relation_id,r.subject_entity_id,r.object_entity_id,r.relation_type,r.meta,
 s.display_name subject_label,s.entity_type subject_kind,
 o.display_name object_label,o.entity_type object_kind
FROM aios.world_entity_relation r
JOIN aios.world_entity s ON s.entity_id=r.subject_entity_id AND s.world_id=r.world_id
JOIN aios.world_entity o ON o.entity_id=r.object_entity_id AND o.world_id=r.world_id
WHERE r.world_id=$3
 AND r.relation_type IN ('located_in','located_on','inside','on','near','part_of','contains','has_part')
 AND (r.meta->>'timeline_id' IS NULL OR r.meta->>'timeline_id'=$2::text)
 AND (r.valid_from_node_id IN (SELECT node_id FROM ancestry)
      OR r.valid_from_node_id IS NULL AND r.meta->>'timeline_id'=$2::text)
 AND (r.valid_to_node_id IS NULL OR r.valid_to_node_id NOT IN (SELECT node_id FROM ancestry))
ORDER BY r.created_at DESC,r.relation_id LIMIT 129
"""
)
_RELATIONSHIPS = (
    _ANCESTRY
    + """
SELECT cr.relationship_id,cr.relationship_type,cr.meta,e.entity_id,e.entity_type,e.display_name,
 EXISTS(SELECT 1 FROM ancestry a JOIN aios.dag_node n ON n.node_id=a.node_id
        JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        WHERE a.node_id::text=cr.meta->>'source_node_id' AND ie.superseded_at IS NULL) encounter_source
FROM aios.character_relationship cr
JOIN aios.world_entity e ON e.entity_id=cr.target_entity_id AND e.world_id=$3
WHERE cr.observer_instance_id=$4 AND
 (cr.meta->>'timeline_id' IS NULL OR cr.meta->>'timeline_id'=$2::text)
 AND (cr.meta->>'source_node_id' IS NULL OR EXISTS (
       SELECT 1 FROM ancestry a JOIN aios.dag_node n ON n.node_id=a.node_id
       JOIN aios.ingest_event ie ON ie.event_id=n.event_id
       WHERE a.node_id::text=cr.meta->>'source_node_id' AND ie.superseded_at IS NULL))
ORDER BY cr.updated_at DESC,cr.relationship_id LIMIT 25
"""
)
# Recheck the same four grant routes and deny precedence as corpus search.
# Exposure is a reference, never proof of learned knowledge or an encounter.
_CORPUS_REFERENCES = """
WITH candidate_sections AS MATERIALIZED (
 SELECT section_id FROM (
   (SELECT ce.section_id,ce.exposed_at seen_at FROM aios.character_corpus_exposure ce
    JOIN aios.character_research_event re ON re.research_id=ce.research_id
    WHERE re.instance_id=$1 ORDER BY ce.exposed_at DESC LIMIT 25)
   UNION ALL
   (SELECT sc.section_id,sc.consumed_at seen_at FROM aios.source_consumption sc
    WHERE sc.instance_id=ANY($3::uuid[]) AND sc.status='ingested'
    ORDER BY sc.consumed_at DESC NULLS LAST LIMIT 25)
 ) recent GROUP BY section_id
), reference_rows AS MATERIALIZED (
 SELECT rp.section_id,rp.payload,rp.updated_at,cs.content,cs.document_id,
 EXISTS(SELECT 1 FROM aios.character_corpus_exposure ce
        JOIN aios.character_research_event re ON re.research_id=ce.research_id
        WHERE ce.section_id=cs.section_id AND re.instance_id=$1) exposed,
 EXISTS(SELECT 1 FROM aios.source_consumption sc
        JOIN aios.knowledge_acquisition_event kae ON kae.consumption_id=sc.consumption_id AND kae.instance_id=sc.instance_id
        JOIN aios.epistemic_authority_admission eaa ON eaa.acquisition_id=kae.acquisition_id
        JOIN aios.semantic_evidence_admission sea ON sea.acquisition_id=kae.acquisition_id AND sea.status='active'
        WHERE sc.section_id=cs.section_id AND sc.instance_id=ANY($3::uuid[])
          AND sc.status='ingested' AND 'reflection'=ANY(eaa.authorized_uses)
          AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)) acquired
 FROM candidate_sections candidate
 JOIN aios.corpus_reference_projection rp ON rp.section_id=candidate.section_id
 JOIN aios.corpus_section cs ON cs.section_id=rp.section_id
 WHERE rp.built_revision=rp.revision AND rp.rdf_revision=rp.revision
)
SELECT r.section_id,r.payload,r.content,r.acquired FROM reference_rows r
JOIN aios.corpus_section cs ON cs.section_id=r.section_id
JOIN aios.corpus_document cd ON cd.document_id=cs.document_id
WHERE (r.exposed OR r.acquired)
 AND (EXISTS(SELECT 1 FROM aios.corpus_document_scope ds JOIN aios.corpus_scope sd
             ON sd.scope_key=ds.scope_key AND sd.access_class='public' WHERE ds.document_id=cd.document_id)
 OR EXISTS(SELECT 1 FROM aios.corpus_document_domain dd JOIN aios.character_knowledge_domain kd
           ON kd.character_id=$2 AND kd.enabled AND kd.knowledge_domain=dd.knowledge_domain
           WHERE dd.document_id=cd.document_id)
 OR EXISTS(SELECT 1 FROM aios.corpus_document_collection dc JOIN aios.corpus_source_profile sp
           ON sp.profile_id=dc.profile_id AND sp.enabled JOIN aios.corpus_scope sd
           ON sd.scope_key=sp.scope_key AND sd.access_class='domain'
           JOIN aios.character_knowledge_domain kd ON kd.character_id=$2 AND kd.enabled
           AND kd.knowledge_domain=sp.knowledge_domain WHERE dc.document_id=cd.document_id)
 OR EXISTS(SELECT 1 FROM aios.corpus_document_scope ds JOIN aios.character_corpus_access ac
           ON ac.character_id=$2 AND ac.allowed AND (ds.scope_key=ac.scope_key
           OR left(ds.scope_key,length(ac.scope_key)+1)=ac.scope_key||'.') WHERE ds.document_id=cd.document_id))
 AND NOT EXISTS(SELECT 1 FROM aios.corpus_document_scope ds JOIN aios.character_corpus_access ac
           ON ac.character_id=$2 AND NOT ac.allowed AND (ds.scope_key=ac.scope_key
           OR left(ds.scope_key,length(ac.scope_key)+1)=ac.scope_key||'.') WHERE ds.document_id=cd.document_id)
ORDER BY r.updated_at DESC,r.section_id LIMIT 25
"""


def _empty(view, graph, **coordinates):
    return {
        "view": view,
        "graph": graph,
        "coordinates": coordinates,
        "entities": [],
        "places": [],
        "people": [],
        "unplaced": [],
        "familiarity": [],
        "truncated": False,
        "complete": False,
        "coverage": "bounded_source_and_world_records",
    }


def _merge(catalog, view, *, mode=None):
    entities = {e["id"]: e for e in catalog["entities"]}
    for entity in view["entities"]:
        entities.setdefault(entity["id"], entity)
    catalog["entities"] = list(entities.values())
    places = {p["place"]["id"]: p for p in catalog["places"]}
    for place in view["places"]:
        if place["place"]["id"] not in places:
            places[place["place"]["id"]] = place
        else:
            current = places[place["place"]["id"]]
            for key in ("contents", "other_evidence"):
                current[key].extend(place[key])
    catalog["places"] = list(places.values())
    if mode:
        catalog["familiarity"].extend(
            {"entity_id": e["id"], "mode": mode, "source": view["source"]}
            for e in view["entities"]
        )


def _finish(catalog):
    placed = {e["entity"]["id"] for p in catalog["places"] for e in p["contents"]}
    catalog["people"] = [e for e in catalog["entities"] if e["kind"] in PERSON_TYPES]
    catalog["unplaced"] = [
        e
        for e in catalog["entities"]
        if e["kind"] not in PLACE_TYPES and e["id"] not in placed
    ]
    return catalog


class EntityCatalogService:
    def __init__(self, db):
        self.db = db

    async def _read(self, read):
        async def snapshot():
            async with self.db.atomic():
                await self.db.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                return await read()

        return await asyncio.wait_for(snapshot(), READ_SECONDS)

    async def world(self, world_id, timeline_id, head_node_id):
        async def read():
            if not await self.db.fetchval(
                """SELECT 1 FROM aios.timeline t JOIN aios.dag_node n
                ON n.timeline_id=t.timeline_id AND n.node_id=$3 WHERE t.world_id=$1 AND t.timeline_id=$2""",
                world_id,
                timeline_id,
                head_node_id,
            ):
                raise LookupError("world, timeline and head must identify one branch")
            catalog = _empty(
                "world",
                f"urn:aios:catalog:world:{world_id}:timeline:{timeline_id}:head:{head_node_id}",
                world_id=str(world_id),
                timeline_id=str(timeline_id),
                head_node_id=str(head_node_id),
            )
            rows = await self.db.fetch(
                _WORLD_EDGES, head_node_id, timeline_id, world_id
            )
            catalog["truncated"] = len(rows) > MAX_EDGES
            entities = {}
            places = {}
            for row in rows[:MAX_EDGES]:
                subject = {
                    "id": world_entity_iri(row["subject_entity_id"]),
                    "label": row["subject_label"] or str(row["subject_entity_id"]),
                    "kind": row["subject_kind"].lower(),
                    "authority": "world_record",
                }
                obj = {
                    "id": world_entity_iri(row["object_entity_id"]),
                    "label": row["object_label"] or str(row["object_entity_id"]),
                    "kind": row["object_kind"].lower(),
                    "authority": "world_record",
                }
                entities.update({subject["id"]: subject, obj["id"]: obj})
                for entity in (subject, obj):
                    if entity["kind"] in PLACE_TYPES:
                        places.setdefault(
                            entity["id"],
                            {"place": entity, "contents": [], "other_evidence": []},
                        )
                member, place = (
                    (obj, subject)
                    if row["relation_type"] in {"contains", "has_part"}
                    else (subject, obj)
                )
                if place["id"] not in places:
                    continue
                edge = {
                    "id": "urn:aios:world-relation:" + str(row["relation_id"]),
                    "entity": member,
                    "place_id": place["id"],
                    "relation": row["relation_type"],
                    "status": "world_record",
                    "source": "urn:aios:world-relation:" + str(row["relation_id"]),
                }
                places[place["id"]][
                    "other_evidence" if row["relation_type"] == "near" else "contents"
                ].append(edge)
            catalog["entities"] = list(entities.values())
            catalog["places"] = list(places.values())
            return _finish(catalog)

        return await self._read(read)

    async def character(self, instance_id):
        async def read():
            from aios_app.hud.context import HUDContextResolver

            ctx = await HUDContextResolver(self.db).resolve(instance_id)
            graph = f"urn:aios:catalog:instance:{instance_id}:scope:{scope_key(ctx)}"
            catalog = _empty(
                "character",
                graph,
                instance_id=str(instance_id),
                cognitive_revision=getattr(ctx, "cognitive_revision", ""),
                world_id=str(ctx.world_id),
                timeline_id=str(ctx.source_timeline_id),
                head_node_id=str(ctx.source_head_node_id),
            )
            catalog["observer"] = "urn:aios:character-instance:" + str(instance_id)
            rows = await self.db.fetch(
                _SOURCE_CATALOG,
                ctx.source_head_node_id,
                ctx.source_timeline_id,
                ctx.character_id,
                owners_for(ctx),
            )
            catalog["truncated"] = len(rows) > MAX_SOURCES
            for row in rows[:MAX_SOURCES]:
                payload = as_json(row["payload"])
                if (
                    payload.get("section_id") != str(row["section_id"])
                    or payload.get("source_sha256")
                    != hashlib.sha256(row["content"].encode()).hexdigest()
                ):
                    continue
                catalog["truncated"] |= bool(payload.get("truncated"))
                _merge(catalog, reference_view(payload), mode="source_mention")
            rows = await self.db.fetch(
                _RELATIONSHIPS,
                ctx.source_head_node_id,
                ctx.source_timeline_id,
                ctx.world_id,
                instance_id,
            )
            catalog["truncated"] |= len(rows) > MAX_SOURCES
            for row in rows[:MAX_SOURCES]:
                entity = {
                    "id": world_entity_iri(row["entity_id"]),
                    "label": row["display_name"] or str(row["entity_id"]),
                    "kind": row["entity_type"].lower(),
                    "authority": "relationship_record",
                }
                source = "urn:aios:character-relationship:" + str(
                    row["relationship_id"]
                )
                mode = (
                    "encounter_record"
                    if row["relationship_type"] == "met" and row["encounter_source"]
                    else "relationship_record"
                )
                _merge(
                    catalog,
                    {"entities": [entity], "places": [], "source": source},
                    mode=mode,
                )
            rows = await self.db.fetch(
                _CORPUS_REFERENCES, instance_id, ctx.character_id, owners_for(ctx)
            )
            catalog["truncated"] |= len(rows) > MAX_SOURCES
            for row in rows[:MAX_SOURCES]:
                payload = as_json(row["payload"])
                if (
                    payload.get("section_id") != str(row["section_id"])
                    or payload.get("source_sha256")
                    != hashlib.sha256(row["content"].encode()).hexdigest()
                ):
                    continue
                catalog["truncated"] |= bool(payload.get("truncated"))
                _merge(
                    catalog,
                    reference_view(payload, corpus=True),
                    mode="acquired_corpus_reference"
                    if row["acquired"]
                    else "corpus_reference_exposure",
                )
            return _finish(catalog)

        return await self._read(read)
