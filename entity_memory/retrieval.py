"""RDF discovers coordinates; current SQL authorizes and supplies every excerpt."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote
from uuid import UUID

from aios_app.entity_memory.service import as_json, graph_iri, payload_hash
from aios_app.epistemic.inquiry.passage import select_passage

log = logging.getLogger(__name__)
POLICY = "rdf-recall-v1"
MAX_REFERENCES = 24
MAX_HITS = 3
# SQL plus transport have separate hard limits. No inference, ANN, or learning.
SQL_BUDGET = 0.35
# Background change detection gets one seven-second window for both reads,
# including connection acquisition. Interactive recall keeps its short budget.
REVISION_SQL_BUDGET = 7.0
TRANSPORT_BUDGET = 0.8
_STOP = set("what where when which why how who whom have has had does did are was were been being there their this that these those with from about would could should will can any some into then than them they your you our and the for not remember recall know seen saw find available resources".split())


def terms_for(query, character_id=""):
    omitted = _STOP | set(re.findall(r"[\w-]+", character_id.casefold()))
    words = list(dict.fromkeys(w for w in re.findall(r"[\w-]+", query.casefold()) if len(w)>=3 and w not in omitted))
    terms = []
    for word in words:
        terms.append(word)
        if word.endswith("s") and len(word)>4 and not word.endswith("ss"):
            terms.append(word[:-1])
    return list(dict.fromkeys(terms))[:6]


def focus_query(focus,goals=()):
    return (focus + " " + " ".join(getattr(g,"text",str(g)) for g in list(goals)[:2]))[:1200]


def owners_for(ctx):
    return list(getattr(ctx,"cognitive_instance_ids",()) or getattr(ctx,"lineage_instance_ids",()) or (ctx.instance_id,))


def scope_key(ctx):
    return hashlib.sha256(json.dumps([POLICY,str(ctx.instance_id),ctx.character_id,
        str(getattr(ctx,"source_timeline_id",None)),str(getattr(ctx,"source_head_node_id",None)),
        str(getattr(ctx,"world_id",None)),sorted(map(str,owners_for(ctx))),getattr(ctx,"cognitive_revision","")],sort_keys=True).encode()).hexdigest()


def query_hash(query, ctx):
    return hashlib.sha256(json.dumps(terms_for(query,ctx.character_id)).encode()).hexdigest()


_ANCESTRY = """WITH RECURSIVE ancestry(node_id,depth) AS (
 SELECT node_id,0 FROM aios.dag_node WHERE node_id=$1 AND timeline_id=$2
 UNION SELECT e.parent_node_id,a.depth+1 FROM ancestry a
 JOIN aios.dag_edge e ON e.child_node_id=a.node_id AND e.timeline_id=$2 WHERE a.depth<256
) """
# Raw source recall is limited to the actual active chat branch. Historical
# continuity requires a source-eligible acquisition in the explicit cognitive scope.
_SOURCE_SELECT = """
SELECT ep.section_id,ep.payload,ep.revision,ep.built_revision,ep.rdf_revision,ep.dataset,
       ds.content,n.node_id,n.timeline_id,n.speaker_id,n.speaker_role::text speaker_role
FROM aios.entity_memory_projection ep
JOIN aios.document_section ds ON ds.section_id=ep.section_id
JOIN aios.dag_node n ON n.node_id=ds.node_id
JOIN aios.ingest_event ie ON ie.event_id=n.event_id
WHERE ep.owner_character_id=$3 AND ep.dataset='char'
 AND ie.superseded_at IS NULL AND ep.built_revision=ep.revision AND ep.rdf_revision=ep.revision
"""
_SOURCE_ACCESS = """ AND (n.timeline_id IS DISTINCT FROM $2 OR n.node_id IN (SELECT node_id FROM ancestry))
 AND ((n.timeline_id=$2 AND n.node_id IN (SELECT node_id FROM ancestry) AND n.kind::text='chat_message')
      OR EXISTS(SELECT 1 FROM aios.knowledge_acquisition_event kae
        JOIN aios.epistemic_authority_admission eaa ON eaa.acquisition_id=kae.acquisition_id
        JOIN aios.semantic_evidence_admission sea ON sea.acquisition_id=kae.acquisition_id AND sea.status='active'
        JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
        JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
        WHERE es.section_id=ds.section_id AND kae.instance_id=ANY($4::uuid[])
          AND 'reflection'=ANY(eaa.authorized_uses)
          AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)))
"""
_SOURCE_SQL = _ANCESTRY + _SOURCE_SELECT + """ AND (cardinality($5::uuid[])>0 AND ep.section_id=ANY($5::uuid[])
      OR cardinality($5::uuid[])=0 AND ep.search_terms && $6::text[])
""" + _SOURCE_ACCESS + "ORDER BY ep.updated_at DESC,ep.section_id LIMIT 12\n"
_LEGACY_ATOM_SQL = _ANCESTRY + """
SELECT DISTINCT ON(p.proposition_id) p.proposition_id,p.atom_id,p.canonical_text,
 p.subject_norm,p.predicate_norm,p.object_norm,p.polarity,p.modality,
 kae.instance_id,kae.claim_id,kae.confidence,kae.epistemic_status,
 eaa.authority_state,eaa.origin_kind,eaa.epistemic_mode,ccr.claim_kind,
 ds.section_id,ds.content,cc.raw_text,n.node_id,n.timeline_id,n.speaker_id
FROM aios.knowledge_acquisition_event kae
JOIN aios.epistemic_authority_admission eaa ON eaa.acquisition_id=kae.acquisition_id
JOIN aios.semantic_evidence_admission sea ON sea.acquisition_id=kae.acquisition_id AND sea.status='active'
JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=kae.claim_id
LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
LEFT JOIN aios.dag_node n ON n.node_id=COALESCE(ds.node_id,kae.dag_node_id)
LEFT JOIN aios.ingest_event ie ON ie.event_id=n.event_id
WHERE $3::text IS NOT NULL AND kae.instance_id=ANY($4::uuid[]) AND 'reflection'=ANY(eaa.authorized_uses)
 AND eaa.authority_state IN ('attested','corroborated','canonical','disputed')
 AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)
 AND (ie.event_id IS NULL OR ie.superseded_at IS NULL)
 AND (n.timeline_id IS DISTINCT FROM $2 OR n.node_id IN (SELECT node_id FROM ancestry))
 AND (cardinality($5::uuid[])>0 AND p.proposition_id=ANY($5::uuid[])
      OR cardinality($5::uuid[])=0 AND EXISTS(SELECT 1 FROM unnest($6::text[]) term
       WHERE lower(concat_ws(' ',p.subject_norm,p.predicate_norm,p.object_norm)) LIKE '%'||term||'%'))
ORDER BY p.proposition_id,kae.created_at DESC LIMIT 12
"""

# Preserve the legacy query only for explicit diagnostic comparison.
_ATOM_SQL = _ANCESTRY.rstrip() + """, atom_candidates AS MATERIALIZED (
 SELECT kae.acquisition_id,kae.proposition_id
 FROM aios.knowledge_acquisition_event kae
 JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
 JOIN aios.epistemic_authority_admission eaa ON eaa.acquisition_id=kae.acquisition_id
 JOIN aios.semantic_evidence_admission sea ON sea.acquisition_id=kae.acquisition_id
 WHERE $3::text IS NOT NULL AND kae.instance_id=ANY($4::uuid[])
   AND sea.status='active' AND 'reflection'=ANY(eaa.authorized_uses)
   AND eaa.authority_state IN ('attested','corroborated','canonical','disputed')
   AND (cardinality($5::uuid[])>0 AND p.proposition_id=ANY($5::uuid[])
        OR cardinality($5::uuid[])=0 AND EXISTS(SELECT 1 FROM unnest($6::text[]) term
         WHERE lower(concat_ws(' ',p.subject_norm,p.predicate_norm,p.object_norm)) LIKE '%'||term||'%'))
)
SELECT DISTINCT ON(p.proposition_id) p.proposition_id,p.atom_id,p.canonical_text,
 p.subject_norm,p.predicate_norm,p.object_norm,p.polarity,p.modality,
 kae.instance_id,kae.claim_id,kae.confidence,kae.epistemic_status,
 eaa.authority_state,eaa.origin_kind,eaa.epistemic_mode,ccr.claim_kind,
 ds.section_id,ds.content,cc.raw_text,n.node_id,n.timeline_id,n.speaker_id
FROM atom_candidates candidate
JOIN aios.knowledge_acquisition_event kae ON kae.acquisition_id=candidate.acquisition_id
JOIN aios.proposition p ON p.proposition_id=candidate.proposition_id
JOIN aios.epistemic_authority_admission eaa ON eaa.acquisition_id=kae.acquisition_id
LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=kae.claim_id
LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
LEFT JOIN aios.dag_node n ON n.node_id=COALESCE(ds.node_id,kae.dag_node_id)
LEFT JOIN aios.ingest_event ie ON ie.event_id=n.event_id
WHERE aios.semantic_acquisition_source_eligible(candidate.acquisition_id)
 AND (ie.event_id IS NULL OR ie.superseded_at IS NULL)
 AND (n.timeline_id IS DISTINCT FROM $2 OR n.node_id IN (SELECT node_id FROM ancestry))
ORDER BY p.proposition_id,kae.created_at DESC LIMIT 12
"""


def literal(value):
    return json.dumps(str(value),ensure_ascii=False)


def entity_query(rows, terms):
    graphs = " ".join("<"+graph_iri(r["section_id"])+">" for r in rows)
    values = " ".join(map(literal,terms))
    return f"""PREFIX em:<urn:aios:entity-memory#>
SELECT DISTINCT ?graph ?hash ?entity ?start ?end ?text WHERE {{
 VALUES ?graph {{ {graphs} }}
 GRAPH ?graph {{
  VALUES ?term {{ {values} }}
  ?source em:payloadHash ?hash ; em:hasAnchor ?seed . ?seed em:label ?label .
  ?source em:hasMention ?mention .
  ?mention em:refersTo ?entity ; em:start ?start ; em:end ?end ; em:text ?text .
  FILTER(CONTAINS(LCASE(STR(?label)),?term))
  FILTER(?entity=?seed || EXISTS {{ ?source em:hasRelationEvidence ?r .
      ?r em:subject ?seed ; em:object ?entity . }} || EXISTS {{
      ?source em:hasRelationEvidence ?r . ?r em:object ?seed ; em:subject ?entity . }})
 }}
}} LIMIT 24"""


def atom_query(ctx,rows):
    atoms=" ".join("<urn:aios:semantic-atom:"+str(r["atom_id"])+">" for r in rows)
    instances=" ".join("<urn:aios:character-instance:"+str(i)+">" for i in owners_for(ctx))
    graph="urn:aios:char:"+quote(ctx.character_id,safe="")+":epistemic"
    return f"""PREFIX char:<urn:aios:char#>
SELECT DISTINCT ?atom WHERE {{ GRAPH <{graph}> {{
 VALUES ?atom {{ {atoms} }} VALUES ?instance {{ {instances} }}
 ?belief char:characterInstance ?instance ; char:semanticAtom ?atom .
}} }} LIMIT 24"""


@dataclass
class RecallResult:
    status: str = "empty"
    hits: list[dict] = field(default_factory=list)
    refs: list[dict] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    trace_id: str | None = None


class RDFRecallService:
    def __init__(self,db,fuseki=None):
        self.db=db
        self.fuseki=fuseki
        self.discovery_diagnostics = {}

    async def _rows(self,ctx,terms,*,sections=(),propositions=(),sources=True):
        return await asyncio.wait_for(self.db.fetch(_SOURCE_SQL if sources else _ATOM_SQL,
            getattr(ctx,"source_head_node_id",None),getattr(ctx,"source_timeline_id",None),
            ctx.character_id,owners_for(ctx),list(sections if sources else propositions),terms),SQL_BUDGET)

    async def _query(self,query):
        from aios_app.config import settings
        from aios_app.rdf.fuseki import FusekiClient
        client=self.fuseki or FusekiClient(settings.fuseki_base_url,timeout=TRANSPORT_BUDGET,retries=0)
        return await asyncio.wait_for(asyncio.to_thread(client.query,"char",query),TRANSPORT_BUDGET+0.1)

    async def discovery_revision(self,ctx,query):
        """Fingerprint eligible coordinates with one background SQL deadline."""
        terms=terms_for(query,ctx.character_id)
        self.discovery_diagnostics = {"stage": "empty_focus", "budget_seconds": REVISION_SQL_BUDGET}
        if not terms:
            return hashlib.sha256(b"empty_focus").hexdigest()
        started = time.monotonic()
        stage = "connection"
        timings = {}
        async def read_coordinates():
            nonlocal stage
            # wait_for owns this task. Both reads share its transaction/session;
            # no nested per-query tasks reserve additional pool connections.
            async with self.db.atomic():
                args = (getattr(ctx,"source_head_node_id",None),
                        getattr(ctx,"source_timeline_id",None),ctx.character_id,
                        owners_for(ctx),[],terms)
                stage = "sources"
                phase = time.monotonic()
                sources = await self.db.fetch(_SOURCE_SQL, *args)
                timings["sources_ms"] = round((time.monotonic()-phase)*1000,1)
                stage = "atoms"
                phase = time.monotonic()
                atoms = await self.db.fetch(_ATOM_SQL, *args)
                timings["atoms_ms"] = round((time.monotonic()-phase)*1000,1)
                return sources, atoms
        try:
            sources, atoms = await asyncio.wait_for(read_coordinates(), REVISION_SQL_BUDGET)
        except Exception as exc:
            elapsed = round((time.monotonic()-started)*1000,1)
            self.discovery_diagnostics = {"stage": stage, "elapsed_ms": elapsed,
                "budget_seconds": REVISION_SQL_BUDGET, "failure": type(exc).__name__, **timings}
            if isinstance(exc, asyncio.TimeoutError):
                # Deadline cancellation is expected, not a second application error.
                log.warning("RDF discovery revision timed out instance=%s stage=%s elapsed_ms=%s budget_seconds=%s",
                            ctx.instance_id, stage, elapsed, REVISION_SQL_BUDGET)
            else:
                log.warning("RDF discovery revision unavailable instance=%s stage=%s reason=%s elapsed_ms=%s",
                            ctx.instance_id, stage, type(exc).__name__, elapsed, exc_info=True)
            return None
        elapsed = round((time.monotonic()-started)*1000,1)
        self.discovery_diagnostics = {"stage": "completed", "elapsed_ms": elapsed,
            "budget_seconds": REVISION_SQL_BUDGET, "source_count": len(sources),
            "atom_count": len(atoms), **timings}
        if elapsed >= SQL_BUDGET*1000:
            log.info("RDF discovery revision slow instance=%s elapsed_ms=%s sources_ms=%s atoms_ms=%s sources=%s atoms=%s",
                     ctx.instance_id, elapsed, timings["sources_ms"], timings["atoms_ms"], len(sources), len(atoms))
        coordinates=[sorted((str(r["section_id"]),r["revision"]) for r in sources),
            sorted((str(r["proposition_id"]),r["authority_state"],r["polarity"],r["modality"],
                    hashlib.sha256((r["content"] or r["canonical_text"]).encode()).hexdigest())
                   for r in atoms),sorted(map(str,owners_for(ctx))),terms]
        return hashlib.sha256(json.dumps(coordinates,sort_keys=True).encode()).hexdigest()

    async def search(self,ctx,query,*,consumer="inquiry",record=True):
        result=RecallResult()
        terms=terms_for(query,ctx.character_id)
        if not terms: return result
        try:
            sources=await self._rows(ctx,terms)
            atoms=await self._rows(ctx,terms,sources=False)
            refs=[]
            if sources:
                found=await self._query(entity_query(sources,terms))
                allowed={graph_iri(r["section_id"]):r for r in sources}
                for binding in found.get("results",{}).get("bindings",[])[:MAX_REFERENCES]:
                    value=lambda key: binding.get(key,{}).get("value","")
                    row=allowed.get(value("graph"))
                    if row is None: continue
                    payload=as_json(row["payload"])
                    if value("hash")!=payload_hash(payload): continue
                    try:
                        start,end=int(value("start")),int(value("end"))
                        UUID(value("entity").removeprefix("urn:aios:memory-entity:"))
                    except (ValueError,TypeError): continue
                    refs.append({"kind":"source","section_id":str(row["section_id"]),
                        "revision":row["revision"],"payload_hash":value("hash"),
                        "entity_id":value("entity").removeprefix("urn:aios:memory-entity:"),
                        "start":start,"end":end,"text":value("text"),"terms":terms})
            if atoms:
                found=await self._query(atom_query(ctx,atoms))
                allowed={}
                for row in atoms:
                    allowed.setdefault("urn:aios:semantic-atom:"+str(row["atom_id"]),[]).append(row)
                for binding in found.get("results",{}).get("bindings",[])[:MAX_REFERENCES]:
                    for row in allowed.get(binding.get("atom",{}).get("value"),[]):
                        refs.append({"kind":"atom","proposition_id":str(row["proposition_id"]),
                                     "atom_id":str(row["atom_id"]),
                            "source_sha256":hashlib.sha256(row["content"].encode()).hexdigest() if row["content"] is not None else None})
            atom_refs=[r for r in refs if r["kind"]=="atom"]
            source_refs=[r for r in refs if r["kind"]=="source"]
            refs=(atom_refs[:12]+source_refs[:12]+atom_refs[12:]+source_refs[12:])[:MAX_REFERENCES]
            result=await self.validate_refs(ctx,refs)
            result.diagnostics.update(candidate_sources=len(sources),candidate_atoms=len(atoms),rdf_references=len(refs))
        except Exception as exc:
            log.info("RDF recall unavailable instance=%s reason=%s",ctx.instance_id,type(exc).__name__)
            result.status="unavailable"
            result.diagnostics["failure"]=type(exc).__name__
        if record:
            try:
                result.trace_id=str(await asyncio.wait_for(self.db.fetchval("""INSERT INTO aios.entity_recall_trace
                        (instance_id,source_head_node_id,scope_key,query_hash,status,refs,diagnostics,consumed_by)
                        VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::text[]) RETURNING trace_id""",
                        ctx.instance_id,getattr(ctx,"source_head_node_id",None),scope_key(ctx),query_hash(query,ctx),
                        result.status,json.dumps(result.refs),json.dumps(result.diagnostics),[consumer]),SQL_BUDGET))
            except Exception:
                log.debug("RDF recall trace unavailable", exc_info=True)
        return result

    async def validate_refs(self,ctx,refs):
        """Rehydrate linked references with current access, lineage and source checks."""
        refs=refs[:MAX_REFERENCES]
        sections=[]; propositions=[]
        for ref in refs:
            try:
                if ref.get("kind")=="source": sections.append(UUID(ref["section_id"]))
                elif ref.get("kind")=="atom": propositions.append(UUID(ref["proposition_id"]))
            except (KeyError,ValueError,TypeError): continue
        source_rows=await self._rows(ctx,[],sections=sections) if sections else []
        atom_rows=await self._rows(ctx,[],propositions=propositions,sources=False) if propositions else []
        sources={str(r["section_id"]):r for r in source_rows}
        atoms={str(r["proposition_id"]):r for r in atom_rows}
        result=RecallResult(); seen=set()
        for ref in refs:
            if len(result.hits)>=MAX_HITS: break
            if ref.get("kind")=="source":
                row=sources.get(ref.get("section_id"))
                if row is None: continue
                payload=as_json(row["payload"])
                content=row["content"]
                if (ref.get("revision")!=row["revision"] or ref.get("payload_hash")!=payload_hash(payload)
                    or payload.get("source_sha256")!=hashlib.sha256(content.encode()).hexdigest()): continue
                mention=next((m for m in payload.get("mentions",[]) if m.get("entity_id")==ref.get("entity_id")
                    and m.get("start")==ref.get("start") and m.get("end")==ref.get("end")
                    and m.get("text")==ref.get("text")),None)
                if mention is None: continue
                start,end=mention["start"],mention["end"]
                if not 0<=start<end<=len(content) or content[start:end]!=mention["text"]: continue
                seed_ids={m["entity_id"] for m in payload.get("mentions",[]) if m.get("entity_id")
                    and any(t in m.get("text","").casefold() or t==m.get("head") for t in ref.get("terms",[]))}
                linked=set(seed_ids)
                for rel in payload.get("relations",[]):
                    if rel["subject"] in seed_ids: linked.add(rel["object"])
                    if rel["object"] in seed_ids: linked.add(rel["subject"])
                if mention["entity_id"] not in linked: continue
                snippet=select_passage(content,anchor_text=mention["text"],source_span=(start,end),budget=400)
                if not snippet.anchor_verified: continue
                key=(str(row["node_id"]),str(row["section_id"]))
                hit={"source":"source_dag","evidence_id":"section:"+str(row["section_id"])+":"+str(start),
                    "text":snippet.text,"durable_knowledge":False,
                    "provenance":{"section_id":str(row["section_id"]),"source_node_id":str(row["node_id"]),
                        "timeline_id":str(row["timeline_id"]),"speaker_id":row["speaker_id"],
                        "offset_basis":"section","start_char":snippet.start_char,"end_char":snippet.end_char,
                        "anchor_verified":True,"retrieval_reason":"rdf_entity_anchor",
                        "authority":"source_report_only","current_world_state":False}}
            elif ref.get("kind")=="atom":
                row=atoms.get(ref.get("proposition_id"))
                if row is None or str(row["atom_id"])!=ref.get("atom_id"): continue
                if row["content"] is not None and ref.get("source_sha256")!=hashlib.sha256(row["content"].encode()).hexdigest():
                    continue
                snippet=select_passage(row["content"] or "",anchor_text=row["raw_text"] or "",budget=400)
                if row["raw_text"] and row["content"] is not None and not snippet.anchor_verified:
                    continue
                text=snippet.text if snippet.anchor_verified else row["canonical_text"][:400]
                key=(str(row["node_id"]),str(row["section_id"])) if row["section_id"] else ("atom",str(row["atom_id"]))
                hit={"source":"char","evidence_id":str(row["proposition_id"]),"text":text,
                     "durable_knowledge":True,"provenance":{"atom_id":str(row["atom_id"]),
                     "source_node_id":str(row["node_id"]) if row["node_id"] else None,
                     "epistemic_status":row["epistemic_status"],"authority_state":row["authority_state"],
                     "polarity":row["polarity"],"modality":row["modality"],"retrieval_reason":"rdf_semantic_atom"},
                     "knowledge":{**dict(row),"text":text,"evidence_instance_id":row["instance_id"],
                         "source_node_id":row["node_id"],"claim_kind":row["claim_kind"] or "BELIEF","retrieval_reason":"rdf_semantic_atom",
                         "effective_confidence":row["confidence"],"origin_kind":row["origin_kind"]}}
            else: continue
            if key in seen: continue
            seen.add(key); result.hits.append(hit); result.refs.append(ref)
        result.status="partial" if result.hits else "empty"
        result.diagnostics={"validated":len(result.hits),"discarded":len(refs)-len(result.refs)}
        return result

    async def cached(self,ctx,query,*,consumer="hud"):
        """No Fuseki on the response/planner path; every cached reference is revalidated."""
        try:
            row=await asyncio.wait_for(self.db.fetchrow("""SELECT trace_id,refs FROM aios.entity_recall_trace
                WHERE instance_id=$1 AND source_head_node_id IS NOT DISTINCT FROM $2
                  AND scope_key=$3 AND query_hash=$4 AND expires_at>now() AND status='partial'
                ORDER BY created_at DESC LIMIT 1""",ctx.instance_id,getattr(ctx,"source_head_node_id",None),
                scope_key(ctx),query_hash(query,ctx)),SQL_BUDGET)
            if not row: return RecallResult()
            result=await self.validate_refs(ctx,as_json(row["refs"]))
            result.trace_id=str(row["trace_id"])
            return result
        except Exception:
            return RecallResult(status="unavailable")
