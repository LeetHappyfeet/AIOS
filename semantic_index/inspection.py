"""Read-only, bounded geometry inspection hydrated from authoritative SQL."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any
from uuid import UUID

import numpy as np

from aios_app.db import Database
from .config import SemanticIndexConfig
from .store import QdrantStore
from .neighbor_classifier import NEIGHBOR_CLASSIFIER_VERSION


def inspection_record(row) -> dict[str, Any]:
    result = dict(row)
    for key in ("meta", "features", "context", "frames"):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    return result


def representatives(vectors: dict[str, list[float]], *, core_ids: set[str] | None = None,
                    limit: int = 8) -> dict[str, Any]:
    """Exact cosine medoid on supplied members; deterministic diverse exemplars.

    Callers report bounded samples. Deduplicate ownership by proposition first.
    """
    ids = sorted(vectors)
    if not ids:
        return {"medoid_id": None, "representative_ids": [], "vector_count": 0}
    matrix = np.asarray([vectors[key] for key in ids], dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1)
    valid = np.isfinite(matrix).all(axis=1) & (norms > 0)
    ids = [key for key, keep in zip(ids, valid) if keep]
    matrix = matrix[valid]
    if not ids:
        return {"medoid_id": None, "representative_ids": [], "vector_count": 0}
    matrix /= norms[valid, None]
    similarities = np.clip(matrix @ matrix.T, -1, 1)
    candidates = [i for i, key in enumerate(ids) if core_ids is None or key in core_ids]
    if not candidates:
        candidates = list(range(len(ids)))
    center = max(candidates, key=lambda i: float(similarities[i, candidates].mean()))
    chosen = [center]
    while len(chosen) < min(max(1, limit), len(ids)):
        remaining = [i for i in range(len(ids)) if i not in chosen]
        chosen.append(min(remaining, key=lambda i: float(similarities[i, chosen].max())))
    return {
        "medoid_id": ids[center], "representative_ids": [ids[i] for i in chosen],
        "vector_count": len(ids), "method": "minimum_total_cosine_distance",
        "mean_similarity_to_medoid": float(similarities[center].mean()),
        "similarity_to_medoid": {key: float(similarities[center, i]) for i, key in enumerate(ids)},
    }


async def inspect_neighborhood(db: Database, *, collection: str | None = None,
                               point_ids: list[UUID] | None = None,
                               cluster_id: UUID | None = None, neighbors: int = 0,
                               limit: int = 200, offset: int = 0,
                               evidence_limit: int = 5, evidence_offset: int = 0,
                               cfg: SemanticIndexConfig | None = None) -> dict[str, Any]:
    cfg = cfg or SemanticIndexConfig()
    if not 1 <= limit <= 512 or not 0 <= neighbors <= 511 or offset < 0:
        raise ValueError("limit must be 1..512, neighbors 0..511, offset nonnegative")
    if not 1 <= evidence_limit <= 20 or evidence_offset < 0:
        raise ValueError("evidence_limit must be 1..20 and evidence_offset nonnegative")
    if bool(cluster_id) == bool(point_ids):
        raise ValueError("provide either cluster_id or point_ids")
    if neighbors:
        if cluster_id or len(set(point_ids or [])) != 1:
            raise ValueError("Neighbor expansion requires one point ID, not a cluster. Select one member as the seed.")
        if offset:
            raise ValueError("Neighbor expansion requires member offset 0.")
    collection = collection or cfg.proposition_collection
    if collection not in {cfg.proposition_collection, cfg.epistemic_collection}:
        raise ValueError("inspection supports proposition and epistemic collections")
    cluster = None
    membership: dict[str, str] = {}
    if cluster_id:
        if collection != cfg.proposition_collection:
            raise ValueError("stored clusters use the proposition collection")
        cluster = await db.fetchrow("SELECT * FROM aios.semantic_cluster_candidate WHERE cluster_id=$1", cluster_id)
        if not cluster:
            raise LookupError("unknown cluster")
        members = await db.fetch("""SELECT proposition_id, membership_kind
            FROM aios.semantic_cluster_membership WHERE cluster_id=$1
            ORDER BY membership_kind, proposition_id LIMIT $2 OFFSET $3""", cluster_id, limit, offset)
        ids = [str(row["proposition_id"]) for row in members]
        membership = {str(row["proposition_id"]): row["membership_kind"] for row in members}
    else:
        if len(point_ids or []) > 512:
            raise ValueError("at most 512 point IDs")
        ids = list(dict.fromkeys(str(value) for value in point_ids or []))[offset:offset+limit]
    if neighbors and len(ids) != 1:
        raise ValueError("neighbor expansion requires exactly one seed point")
    store = QdrantStore(cfg.qdrant_url, cfg.qdrant_api_key, collection, 0)
    def read_points():
        points = store.client.retrieve(collection_name=collection, ids=ids,
                                       with_payload=True, with_vectors=True) if ids else []
        scores: dict[str, float] = {}
        if neighbors and points:
            vector = points[0].vector
            if isinstance(vector, dict):
                vector = next(iter(vector.values()))
            hits = store.search(list(vector), top_k=min(limit, neighbors + 1))
            scores = {key: score for key, score, _ in hits}
            expanded = list(dict.fromkeys(ids + list(scores)))[:limit]
            points = store.client.retrieve(collection_name=collection, ids=expanded,
                                           with_payload=True, with_vectors=True)
        return points, scores
    try:
        points, scores = await asyncio.to_thread(read_points)
        points = sorted(points, key=lambda point: str(point.id))
    finally:
        store.client.close()
    mapped: dict[str, list[dict[str, Any]]] = {}
    vectors: dict[str, list[float]] = {}
    found = set()
    for point in points:
        key = str(point.id)
        found.add(key)
        payload = dict(point.payload or {})
        proposition = payload.get("proposition_id")
        if not proposition:
            continue
        proposition = str(UUID(str(proposition)))
        mapped.setdefault(proposition, []).append({"point_id": key, "payload": payload,
                                                   "seed_similarity": scores.get(key)})
        vector = point.vector
        if isinstance(vector, dict):
            vector = next(iter(vector.values()))
        # Deterministic choice for ownership copies with different status embeddings.
        if proposition not in vectors and vector is not None:
            vectors[proposition] = list(vector)
    # A missing vector must not hide the SQL member of a stored cluster.
    proposition_ids = list(dict.fromkeys(ids if cluster_id else mapped))
    uuids = [UUID(key) for key in proposition_ids]
    rows = await db.fetch("SELECT * FROM aios.proposition WHERE proposition_id=ANY($1::uuid[]) ORDER BY proposition_id", uuids)
    evidence = await db.fetch("""
        SELECT p.proposition_id, e.* FROM aios.proposition p
        CROSS JOIN LATERAL (
            SELECT o.observation_id, o.claim_id, o.source_key, o.source_domain,
                   o.source_kind, o.observed_at, o.dag_node_id,
                   cc.raw_text, es.sentence_text, es.section_id,
                   left(ds.content,12000) AS source_text, length(ds.content) AS source_text_length,
                   sd.title, sd.source_url,
                   left(dn.message_text,12000) AS message_text,
                   length(dn.message_text) AS message_text_length,
                   to_jsonb(ccr) AS context,
                   (SELECT jsonb_agg(to_jsonb(f) ORDER BY f.frame_index)
                    FROM (SELECT * FROM aios.claim_semantic_frame
                          WHERE claim_id=o.claim_id ORDER BY frame_index LIMIT 32) f) AS frames,
                   (SELECT count(*) FROM aios.claim_semantic_frame f WHERE f.claim_id=o.claim_id) AS frame_count,
                   count(*) OVER () AS evidence_count
            FROM aios.observation o
            LEFT JOIN aios.claim_candidate cc ON cc.claim_id=o.claim_id
            LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
            LEFT JOIN aios.source_document sd ON sd.document_id=COALESCE(o.document_id,ds.document_id)
            LEFT JOIN aios.dag_node dn ON dn.node_id=o.dag_node_id
            LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
            WHERE o.proposition_id=p.proposition_id
            ORDER BY o.observed_at DESC, o.observation_id
            LIMIT $2 OFFSET $3
        ) e WHERE p.proposition_id=ANY($1::uuid[])
    """, uuids, evidence_limit, evidence_offset)
    edges = await db.fetch("""
        SELECT c.proposition_id, c.neighbor_proposition_id, c.similarity,
               r.relation, r.confidence, r.features,
               d.status AS validation_status, d.selected_value,
               a.canonical_text AS text_a, b.canonical_text AS text_b,
               a.subject_norm AS subject_a, b.subject_norm AS subject_b,
               a.predicate_norm AS predicate_a, b.predicate_norm AS predicate_b,
               a.object_norm AS object_a, b.object_norm AS object_b,
               a.polarity AS polarity_a, b.polarity AS polarity_b
        FROM aios.semantic_neighbor_candidate c
        LEFT JOIN aios.semantic_neighbor_relation r
          ON r.proposition_id=c.proposition_id AND r.neighbor_proposition_id=c.neighbor_proposition_id
         AND r.embedding_version=c.embedding_version AND r.classifier_version=$2
        LEFT JOIN aios.semantic_validation_decision d
          ON d.decision_type=CASE WHEN r.relation='SAME_EVENT' OR r.features->>'both_events'='true'
                                      THEN 'event_identity' ELSE 'proposition_relation' END
         AND d.decision_key=c.proposition_id::text || ':' || c.neighbor_proposition_id::text
        JOIN aios.proposition a ON a.proposition_id=c.proposition_id
        JOIN aios.proposition b ON b.proposition_id=c.neighbor_proposition_id
        WHERE c.embedding_version=$3 AND c.status='candidate'
          AND (c.proposition_id=ANY($1::uuid[]) OR c.neighbor_proposition_id=ANY($1::uuid[]))
        ORDER BY (r.relation='CONTRADICTS') DESC NULLS LAST, c.similarity DESC,
                 c.proposition_id, c.neighbor_proposition_id LIMIT 2001
    """, uuids, NEIGHBOR_CLASSIFIER_VERSION, cfg.embedding_version)
    by_proposition: dict[str, list[dict[str, Any]]] = {}
    for row in evidence:
        item = inspection_record(row)
        item["source_available"] = bool(item.get("source_text") or item.get("message_text"))
        by_proposition.setdefault(str(row["proposition_id"]), []).append(item)
    selected = set(proposition_ids)
    edge_items = []
    for row in edges[:2000]:
        item = inspection_record(row)
        item["boundary"] = not {str(row["proposition_id"]), str(row["neighbor_proposition_id"])} <= selected
        item["structural_differences"] = [field for field in ("subject", "predicate", "object", "polarity")
                                          if item[field + "_a"] != item[field + "_b"]]
        edge_items.append(item)
    sample = representatives(vectors, core_ids={key for key, kind in membership.items() if kind == "core"} or None)
    sample["sampled"] = bool(cluster and cluster["member_count"] > len(proposition_ids))
    sample["missing_vector_count"] = len(set(proposition_ids) - set(vectors))
    sample["contrast_proposition_ids"] = sorted({str(item[key]) for item in edge_items
        if item["relation"] == "CONTRADICTS" for key in ("proposition_id", "neighbor_proposition_id")})
    sample["fringe_proposition_ids"] = sorted(key for key, kind in membership.items() if kind == "fringe")
    sample["ownership_vector_policy"] = "one retrieved vector per distinct proposition"
    return {
        "collection": collection, "cluster": inspection_record(cluster) if cluster else None,
        "point_count": len(points), "distinct_proposition_count": len(proposition_ids),
        "missing_point_ids": sorted(set(ids) - found),
        "representatives": sample,
        "members": [dict(inspection_record(row), points=mapped.get(str(row["proposition_id"]), []),
                         membership_kind=membership.get(str(row["proposition_id"])),
                         evidence=by_proposition.get(str(row["proposition_id"]), [])) for row in rows],
        "edges": edge_items, "edges_truncated": len(edges) > 2000,
        "relation_counts": dict(Counter(item["relation"] or "UNCLASSIFIED" for item in edge_items)),
        "source_excerpt_limit": 12000, "frame_limit_per_observation": 32,
        "pagination": {"limit": limit, "offset": offset, "evidence_limit": evidence_limit,
                       "evidence_offset": evidence_offset},
        "missing_proposition_ids": sorted(selected - {str(row["proposition_id"]) for row in rows}),
    }
