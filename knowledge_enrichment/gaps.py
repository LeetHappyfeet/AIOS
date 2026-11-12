"""Deterministic, opt-in structural gap discovery for cold corpus.

No model calls; no RDF, vector, claim, knowledge, or HUD mutations.
The evidence ledger is advisory and only exposes metadata from the corpus.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from itertools import combinations
from typing import Any
from uuid import UUID

VERSION = "structure-gap-shadow-v1"
PAIR_THRESHOLD = 0.78
MIN_DISTINCT_DOCUMENTS = 2
MAX_SCAN_SECTIONS = 500


def revision_key(items: list[tuple[str, str]]) -> str:
    return hashlib.sha256(json.dumps(sorted(items), separators=(",", ":")).encode()).hexdigest()


def candidate_pairs(rows: list[dict], *, minimum_documents: int = MIN_DISTINCT_DOCUMENTS) -> list[dict]:
    """Only pairs co-occurring in at least two distinct source documents qualify.

    Caller must pre-filter source projection revisions, domain and topic status.
    """
    by_section: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if float(row["similarity"] or 0) < PAIR_THRESHOLD:
            continue
        by_section[(str(row["domain_key"]), str(row["section_id"]))].append(row)

    pairs: dict[tuple[str, str, str], dict[str, Any]] = {}
    for (domain, section), links in by_section.items():
        # One record per topic; defend against duplicate input rows.
        links = list({str(r["topic_id"]): r for r in links}.values())[:8]
        for left, right in combinations(links, 2):
            a, b = sorted((str(left["topic_id"]), str(right["topic_id"])))
            key = (domain, a, b)
            record = pairs.setdefault(key, {
                "domain_key": domain, "topic_a": a, "topic_b": b,
                "sections": set(), "documents": set(), "revisions": [],
                "scores": [],
            })
            record["sections"].add(section)
            record["documents"].add(str(left["document_id"]))
            record["revisions"].append((
                section, str(left["source_fingerprint"]) + ":" + str(left["reference_revision"])
            ))
            record["scores"].append(min(float(left["similarity"]), float(right["similarity"])))
    result = []
    for item in pairs.values():
        if len(item["documents"]) < minimum_documents:
            continue
        result.append({
            "domain_key": item["domain_key"], "topic_a": item["topic_a"], "topic_b": item["topic_b"],
            "sections": sorted(item["sections"]), "documents": sorted(item["documents"]),
            "revision": revision_key(item["revisions"]),
            "signals": {
                "distinct_documents": len(item["documents"]),
                "matching_sections": len(item["sections"]),
                "minimum_pair_similarity": round(min(item["scores"]), 4),
                "trigger": "independent_documents_with_unresolved_topic_association",
            },
        })
    return sorted(result, key=lambda r: (
        -r["signals"]["distinct_documents"], -r["signals"]["matching_sections"],
        r["domain_key"], r["topic_a"], r["topic_b"]
    ))


async def mark_stale_gaps(db, *, domain_key: str, limit: int = 200) -> int:
    """Invalidate old source snapshots without relying on a bounded rescan.

    This checks changed/deleted source sections, not whether a relation has
    since become verified. Such questions remain candidates for later review.
    """
    rows = await db.fetch(
        """SELECT gap_id,evidence_sections,evidence_revision
           FROM aios.corpus_structure_gap
           WHERE domain_key=$1 AND status='candidate'
           ORDER BY last_seen_at DESC,gap_id LIMIT $2""",
        domain_key,max(1,min(int(limit),500)),
    )
    if not rows:
        return 0
    section_ids = list({sid for row in rows for sid in row["evidence_sections"]})
    evidence = await db.fetch(
        """SELECT cs.section_id,cd.source_fingerprint,
                  rp.built_revision AS reference_revision,
                  (rp.built_revision=rp.revision AND
                   rp.rdf_revision=rp.built_revision) AS reference_ready
           FROM aios.corpus_section cs
           JOIN aios.corpus_discovery_projection cd ON cd.section_id=cs.section_id
           JOIN aios.corpus_reference_projection rp ON rp.section_id=cs.section_id
           JOIN aios.corpus_document_domain dd ON dd.document_id=cs.document_id
           WHERE dd.knowledge_domain=$1 AND cs.section_id=ANY($2::uuid[])""",
        domain_key,section_ids,
    )
    current = {str(r["section_id"]):r for r in evidence}
    stale_ids = []
    for gap in rows:
        revisions = []
        for sid in gap["evidence_sections"]:
            row = current.get(str(sid))
            if not row or not row["reference_ready"]:
                break
            revisions.append((
                str(sid),str(row["source_fingerprint"])+":"+str(row["reference_revision"])
            ))
        else:
            if revision_key(revisions) == gap["evidence_revision"]:
                continue
        stale_ids.append(gap["gap_id"])
    if stale_ids:
        await db.execute(
            """UPDATE aios.corpus_structure_gap
               SET status='stale'
               WHERE gap_id=ANY($1::uuid[]) AND status='candidate'""",
            stale_ids,
        )
    return len(stale_ids)


async def scan_structure_gaps(db, *, domain_key: str, limit: int = 200) -> dict:
    """Scan a bounded domain sample; deliberately operator-initiated in V1.

    No global scans or continuous enqueue. At most MAX_SCAN_SECTIONS per request.
    """
    if not domain_key:
        raise ValueError("Choose a knowledge domain first")
    limit = max(1, min(int(limit), MAX_SCAN_SECTIONS))
    exists = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM aios.knowledge_domain WHERE domain_key=$1 AND enabled)",
        domain_key,
    )
    if not exists:
        raise ValueError("Unknown or disabled knowledge domain")
    # Capture source snapshot first. Latest successful Qdrant and RDF receipts
    # must match the linked section's revision; stale links do not qualify.
    rows = await db.fetch(
        """WITH picked AS (
            SELECT cs.section_id, cs.document_id, cd.source_fingerprint,
                   rp.built_revision AS reference_revision, rp.payload,
                   l.source_fingerprint AS linked_fingerprint
            FROM aios.corpus_section cs
            JOIN aios.corpus_document_domain dd ON dd.document_id=cs.document_id
            JOIN aios.corpus_discovery_projection cd ON cd.section_id=cs.section_id
            JOIN aios.corpus_topic_link_state l ON l.section_id=cs.section_id
                 AND l.source_fingerprint=cd.source_fingerprint
            JOIN aios.corpus_reference_projection rp ON rp.section_id=cs.section_id
                 AND rp.revision=rp.built_revision
                 AND rp.rdf_revision=rp.built_revision
            WHERE dd.knowledge_domain=$1
              AND l.source_fingerprint=cd.source_fingerprint
            ORDER BY cd.indexed_at DESC,cs.section_id
            LIMIT $2
        )
        SELECT $1::text AS domain_key, p.section_id,p.document_id,
               p.source_fingerprint,p.reference_revision,
               t.topic_id,ts.similarity
        FROM picked p
        JOIN aios.knowledge_topic_source ts ON ts.section_id=p.section_id
             AND ts.link_kind='vector_candidate' AND ts.status='candidate'
             AND ts.source_revision=p.source_fingerprint
        JOIN aios.knowledge_topic t ON t.topic_id=ts.topic_id
             AND t.visibility='catalog' AND t.namespace='catalog:' || $1
             AND t.status IN ('candidate','registered','organized')
        WHERE ts.similarity >= $3
        ORDER BY p.section_id,ts.similarity DESC""",
        domain_key, limit, PAIR_THRESHOLD,
    )
    pair_candidates = candidate_pairs([dict(row) for row in rows])
    stale_marked = await mark_stale_gaps(db,domain_key=domain_key)
    pair_created = 0
    pair_updated = 0
    skipped_verified = 0
    for item in pair_candidates[:100]:
        # Verified topic relations are not structural gaps. Check both directions.
        verified = await db.fetchval(
            """SELECT EXISTS (
               SELECT 1 FROM aios.knowledge_topic_relation
               WHERE status='verified'
                 AND ((source_topic_id=$1 AND target_topic_id=$2)
                   OR (source_topic_id=$2 AND target_topic_id=$1)))""",
            UUID(item["topic_a"]), UUID(item["topic_b"]),
        )
        if verified:
            skipped_verified += 1
            continue
        result = await db.fetchrow(
            """INSERT INTO aios.corpus_structure_gap (
                  domain_key,trigger_kind,topic_a,topic_b,
                  evidence_sections,evidence_documents,evidence_revision,signals)
               VALUES ($1,'semantic_unresolved_pair',$2,$3,$4,$5,$6,$7::jsonb)
               ON CONFLICT (domain_key,topic_a,topic_b)
               WHERE trigger_kind='semantic_unresolved_pair'
               DO UPDATE SET evidence_sections=EXCLUDED.evidence_sections,
                  evidence_documents=EXCLUDED.evidence_documents,
                  evidence_revision=EXCLUDED.evidence_revision,
                  signals=EXCLUDED.signals,last_seen_at=now(),
                  scan_version=$8,
                  status=CASE WHEN aios.corpus_structure_gap.status='dismissed'
                         THEN 'dismissed' ELSE 'candidate' END
               RETURNING gap_id,(xmax=0) AS inserted""",
            item["domain_key"],UUID(item["topic_a"]),UUID(item["topic_b"]),
            [UUID(s) for s in item["sections"]], [UUID(d) for d in item["documents"]],
            item["revision"],json.dumps(item["signals"]),VERSION,
        )
        pair_created += int(bool(result["inserted"]))
        pair_updated += int(not result["inserted"])

    # Second candidate: a rich reference section lacking usable relationships.
    # Probe only the same bounded source population; never parse another page.
    dense_rows = await db.fetch(
        """SELECT cs.section_id,cs.document_id, cd.source_fingerprint,
                  rp.built_revision AS reference_revision,rp.payload
           FROM aios.corpus_section cs
           JOIN aios.corpus_document_domain dd ON dd.document_id=cs.document_id
           JOIN aios.corpus_discovery_projection cd ON cd.section_id=cs.section_id
           JOIN aios.corpus_reference_projection rp ON rp.section_id=cs.section_id
              AND rp.built_revision=rp.revision AND rp.rdf_revision=rp.revision
           WHERE dd.knowledge_domain=$1 AND length(cs.content)>=800
           ORDER BY cd.indexed_at DESC,cs.section_id LIMIT $2""", domain_key, limit,
    )
    dense_created = 0
    dense_updated = 0
    for row in dense_rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict) or payload.get("parse_status") != "ready" or payload.get("truncated"):
            continue
        anchors = payload.get("anchors") or []
        relations = payload.get("relations") or []
        if not isinstance(anchors, list) or not isinstance(relations, list):
            continue
        if len(anchors) < 6 or len(relations) > 1:
            continue
        fingerprint = revision_key([
            (str(row["section_id"]), str(row["source_fingerprint"]) + ":" + str(row["reference_revision"]))
        ])
        signals = {
            "anchor_count": len(anchors), "relation_count": len(relations),
            "trigger": "dense_reference_with_sparse_relations",
        }
        result = await db.fetchrow(
            """INSERT INTO aios.corpus_structure_gap (
                domain_key,trigger_kind,anchor_section_id,
                evidence_sections,evidence_documents,evidence_revision,signals)
               VALUES ($1,'dense_sparse_reference',$2,$3,$4,$5,$6::jsonb)
               ON CONFLICT (domain_key,anchor_section_id)
               WHERE trigger_kind='dense_sparse_reference'
               DO UPDATE SET evidence_sections=EXCLUDED.evidence_sections,
                  evidence_documents=EXCLUDED.evidence_documents,
                  evidence_revision=EXCLUDED.evidence_revision,
                  signals=EXCLUDED.signals,last_seen_at=now(),
                  scan_version=$7,
                  status=CASE WHEN aios.corpus_structure_gap.status='dismissed'
                       THEN 'dismissed' ELSE 'candidate' END
               RETURNING gap_id,(xmax=0) AS inserted""",
            domain_key,row["section_id"],[row["section_id"]],[row["document_id"]],
            fingerprint,json.dumps(signals),VERSION,
        )
        dense_created += int(bool(result["inserted"]))
        dense_updated += int(not result["inserted"])
    return {
        "domain": domain_key, "version": VERSION, "sample_limit": limit,
        "linked_topic_rows": len(rows), "pair_candidates": len(pair_candidates),
        "stale_prior_candidates": stale_marked,
        "pairs_inserted": pair_created, "pairs_refreshed": pair_updated,
        "verified_pairs_skipped": skipped_verified,
        "dense_sections_examined": len(dense_rows),
        "dense_inserted": dense_created, "dense_refreshed": dense_updated,
        "llm_calls": 0, "rdf_updates": 0, "vector_updates": 0,
        "note": "Only a bounded eligible-source sample was scanned; no claim of exhaustive coverage.",
    }


async def list_structure_gaps(db, *, domain_key: str, limit: int = 100) -> list[dict]:
    rows = await db.fetch(
        """SELECT g.gap_id,g.trigger_kind,g.status,g.last_seen_at,
                  g.evidence_sections,g.evidence_documents,g.signals,
                  a.display_label AS topic_a_label,b.display_label AS topic_b_label,
                  cs.heading AS anchor_heading,
                  g.evidence_revision
           FROM aios.corpus_structure_gap g
           LEFT JOIN aios.knowledge_topic a ON a.topic_id=g.topic_a
           LEFT JOIN aios.knowledge_topic b ON b.topic_id=g.topic_b
           LEFT JOIN aios.corpus_section cs ON cs.section_id=g.anchor_section_id
           WHERE g.domain_key=$1
           ORDER BY g.last_seen_at DESC,g.gap_id
           LIMIT $2""", domain_key, max(1,min(int(limit),500)),
    )
    return [
        {"gap_id":str(r["gap_id"]),"kind":r["trigger_kind"],"status":r["status"],
         "subject":r["topic_a_label"] or r["anchor_heading"] or "",
         "related":r["topic_b_label"] or "",
         "section_count":len(r["evidence_sections"] or []),
         "document_count":len(r["evidence_documents"] or []),
         "last_seen":r["last_seen_at"].isoformat(),
         "revision":r["evidence_revision"][:12],
         "signals":dict(r["signals"]) if isinstance(r["signals"],dict) else r["signals"]}
        for r in rows
    ]


async def structure_gap_summary(db, *, domain_key: str) -> dict:
    rows = await db.fetch(
        """SELECT trigger_kind,status,count(*) AS n
           FROM aios.corpus_structure_gap
           WHERE domain_key=$1 GROUP BY trigger_kind,status
           ORDER BY trigger_kind,status""",domain_key,
    )
    return {
        "domain":domain_key,
        "counts":{f"{r['trigger_kind']}:{r['status']}":int(r["n"]) for r in rows},
        "authority":"advisory_shadow_only",
        "llm_enabled":False,
        "live_hud_effect":"none",
    }
