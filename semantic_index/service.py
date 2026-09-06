from __future__ import annotations

import logging
from uuid import uuid5, NAMESPACE_URL

from qdrant_client.http import models as qm

from aios_app.db import Database
from .config import SemanticIndexConfig
from .embeddings import Embedder, stable_text_hash
from .store import QdrantStore

logger = logging.getLogger("aios.semantic_index")

_EMBEDDER: Embedder | None = None
_STORES: dict[str, QdrantStore] = {}


def _get_embedder(cfg: SemanticIndexConfig) -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        logger.info("Initializing semantic embedder [%s | device=%s]", cfg.embedding_model, cfg.embedding_device)
        _EMBEDDER = Embedder(cfg.embedding_model, cfg.embedding_device)
    return _EMBEDDER


def _get_store(cfg: SemanticIndexConfig, collection: str) -> QdrantStore:
    if collection not in _STORES:
        embedder = _get_embedder(cfg)
        logger.info("Initializing Qdrant semantic collection [%s]", collection)
        store = QdrantStore(cfg.qdrant_url, cfg.qdrant_api_key, collection, embedder.dim)
        store.ensure_collection()
        _STORES[collection] = store
    return _STORES[collection]


def initialize_backend(cfg: SemanticIndexConfig, *, warmup: bool = True) -> None:
    embedder = _get_embedder(cfg)
    for collection in (
        cfg.source_collection,
        cfg.proposition_collection,
        cfg.epistemic_collection,
    ):
        _get_store(cfg, collection)

    if (
        cfg.drop_legacy_rag_collection
        and cfg.legacy_rag_collection
        and cfg.legacy_rag_collection not in {
            cfg.source_collection,
            cfg.proposition_collection,
            cfg.epistemic_collection,
        }
    ):
        client = _get_store(cfg, cfg.source_collection).client
        try:
            client.get_collection(cfg.legacy_rag_collection)
        except Exception:
            pass
        else:
            logger.info(
                "Removing retired legacy RAG Qdrant collection [%s]",
                cfg.legacy_rag_collection,
            )
            client.delete_collection(cfg.legacy_rag_collection)

    if warmup:
        vector = embedder.embed(["AIOS semantic index warmup"])[0]
        if len(vector) != embedder.dim:
            raise RuntimeError("semantic embedding dimension mismatch")
    logger.info(
        "Semantic index initialized [model=%s dim=%d source=%s propositions=%s epistemic=%s]",
        cfg.embedding_model,
        embedder.dim,
        cfg.source_collection,
        cfg.proposition_collection,
        cfg.epistemic_collection,
    )


async def _mark_indexed(
    db: Database,
    cfg: SemanticIndexConfig,
    *,
    object_type: str,
    object_key: str,
    collection: str,
    vector_hash: str,
) -> None:
    await db.execute(
        """
        INSERT INTO aios.semantic_vector_index_state (
            object_type, object_key, qdrant_collection,
            embedding_model, embedding_version, vector_hash,
            indexed_at, last_error
        )
        VALUES ($1,$2,$3,$4,$5,$6,now(),NULL)
        ON CONFLICT (
            object_type, object_key, qdrant_collection,
            embedding_model, embedding_version
        )
        DO UPDATE SET
            vector_hash=EXCLUDED.vector_hash,
            indexed_at=now(),
            last_error=NULL
        """,
        object_type, object_key, collection,
        cfg.embedding_model, cfg.embedding_version, vector_hash,
    )


async def index_source_sections_once(db: Database, cfg: SemanticIndexConfig) -> int:
    rows = await db.fetch(
        """
        SELECT
            ds.section_id, ds.content, ds.document_id, ds.node_id,
            ds.section_path, ds.section_order,
            dn.event_id, dn.timeline_id, dn.character_id, dn.viewpoint_id,
            dn.created_at,
            t.world_id,
            sd.source_type, sd.source_url,
            ie.source_id, ie.source_kind
        FROM aios.document_section ds
        JOIN aios.dag_node dn ON dn.node_id=ds.node_id
        LEFT JOIN aios.timeline t ON t.timeline_id=dn.timeline_id
        LEFT JOIN aios.source_document sd ON sd.document_id=ds.document_id
        LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE ds.content IS NOT NULL
          AND length(ds.content) > 0
          AND NOT EXISTS (
              SELECT 1 FROM aios.semantic_vector_index_state s
              WHERE s.object_type='source_section'
                AND s.object_key=ds.section_id::text
                AND s.qdrant_collection=$2
                AND s.embedding_model=$3
                AND s.embedding_version=$4
          )
        ORDER BY dn.created_at
        LIMIT $1
        """,
        cfg.batch_size, cfg.source_collection,
        cfg.embedding_model, cfg.embedding_version,
    )
    if not rows:
        return 0

    texts = [r["content"] for r in rows]
    hashes = [stable_text_hash(t) for t in texts]
    vectors = _get_embedder(cfg).embed(texts)
    points: list[qm.PointStruct] = []
    for row, vector, vector_hash in zip(rows, vectors, hashes):
        source_url = row["source_url"]
        source_domain = None
        if source_url and "://" in source_url:
            source_domain = source_url.split("://", 1)[1].split("/", 1)[0].lower()
        payload = {
            "object_type": "source_section",
            "section_id": str(row["section_id"]),
            "document_id": str(row["document_id"]) if row["document_id"] else None,
            "node_id": str(row["node_id"]) if row["node_id"] else None,
            "event_id": row["event_id"],
            "timeline_id": str(row["timeline_id"]) if row["timeline_id"] else None,
            "world_id": str(row["world_id"]) if row["world_id"] else None,
            "character_id": row["character_id"],
            "viewpoint_id": row["viewpoint_id"],
            "source_id": row["source_id"],
            "source_type": row["source_type"] or row["source_kind"],
            "source_domain": source_domain,
            "section_path": row["section_path"],
            "section_order": row["section_order"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "embedding_version": cfg.embedding_version,
            "vector_hash": vector_hash,
        }
        points.append(qm.PointStruct(
            id=str(row["section_id"]),
            vector=vector,
            payload={k:v for k,v in payload.items() if v is not None},
        ))
    _get_store(cfg, cfg.source_collection).upsert(points)
    for row, vector_hash in zip(rows, hashes):
        await _mark_indexed(
            db, cfg, object_type="source_section",
            object_key=str(row["section_id"]),
            collection=cfg.source_collection, vector_hash=vector_hash,
        )
    logger.info("Indexed %d source sections into Qdrant [%s]", len(rows), cfg.source_collection)
    return len(rows)


async def index_propositions_once(db: Database, cfg: SemanticIndexConfig) -> int:
    rows = await db.fetch(
        """
        SELECT
            p.proposition_id, p.topic_key, p.subject_norm, p.predicate_norm,
            p.object_norm, p.polarity, p.modality, p.canonical_text, p.created_at,
            ctx.claim_kind, ctx.predicate_family, ctx.subject_kind, ctx.object_kind,
            ctx.origin_character_id, ctx.character_instance_id,
            ctx.world_id, ctx.timeline_id, ctx.dag_node_id, ctx.epistemic_scope
        FROM aios.proposition p
        LEFT JOIN LATERAL (
            SELECT ccr.*
            FROM aios.observation o
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
            WHERE o.proposition_id=p.proposition_id
            ORDER BY ccr.resolved_at DESC
            LIMIT 1
        ) ctx ON true
        WHERE NOT EXISTS (
            SELECT 1 FROM aios.semantic_vector_index_state s
            WHERE s.object_type='proposition'
              AND s.object_key=p.proposition_id::text
              AND s.qdrant_collection=$2
              AND s.embedding_model=$3
              AND s.embedding_version=$4
        )
        ORDER BY p.created_at
        LIMIT $1
        """,
        cfg.batch_size, cfg.proposition_collection,
        cfg.embedding_model, cfg.embedding_version,
    )
    if not rows:
        return 0

    texts = [
        " | ".join(filter(None, [
            f"topic: {r['topic_key']}",
            f"subject: {r['subject_norm']}" if r["subject_norm"] else None,
            f"predicate: {r['predicate_norm']}" if r["predicate_norm"] else None,
            f"object: {r['object_norm']}" if r["object_norm"] else None,
            f"kind: {r['claim_kind']}" if r["claim_kind"] else None,
            f"family: {r['predicate_family']}" if r["predicate_family"] else None,
            f"text: {r['canonical_text']}",
        ]))
        for r in rows
    ]
    hashes = [stable_text_hash(t) for t in texts]
    vectors = _get_embedder(cfg).embed(texts)
    points = []
    for row, vector, vector_hash in zip(rows, vectors, hashes):
        payload = {
            "object_type": "proposition",
            "proposition_id": str(row["proposition_id"]),
            "topic_key": row["topic_key"],
            "claim_kind": row["claim_kind"],
            "predicate_family": row["predicate_family"],
            "subject_kind": row["subject_kind"],
            "object_kind": row["object_kind"],
            "origin_character_id": row["origin_character_id"],
            "instance_id": str(row["character_instance_id"]) if row["character_instance_id"] else None,
            "world_id": str(row["world_id"]) if row["world_id"] else None,
            "timeline_id": str(row["timeline_id"]) if row["timeline_id"] else None,
            "node_id": str(row["dag_node_id"]) if row["dag_node_id"] else None,
            "epistemic_scope": row["epistemic_scope"],
            "polarity": int(row["polarity"]),
            "modality": row["modality"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "embedding_version": cfg.embedding_version,
            "vector_hash": vector_hash,
        }
        points.append(qm.PointStruct(
            id=str(row["proposition_id"]), vector=vector,
            payload={k:v for k,v in payload.items() if v is not None},
        ))
    _get_store(cfg, cfg.proposition_collection).upsert(points)
    for row, vector_hash in zip(rows, hashes):
        await _mark_indexed(
            db, cfg, object_type="proposition",
            object_key=str(row["proposition_id"]),
            collection=cfg.proposition_collection, vector_hash=vector_hash,
        )
    logger.info("Indexed %d normalized propositions into Qdrant [%s]", len(rows), cfg.proposition_collection)
    return len(rows)


def _epistemic_point_id(object_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, "urn:aios:epistemic:" + object_key))


async def index_epistemic_objects_once(db: Database, cfg: SemanticIndexConfig) -> int:
    rows = await db.fetch(
        """
        SELECT * FROM (
            SELECT
                'character_knowledge'::text AS object_type,
                (ck.instance_id::text || ':' || ck.proposition_id::text) AS object_key,
                ck.instance_id, ci.character_id, ci.world_id,
                ck.proposition_id, ck.epistemic_status, ck.acquisition_mode,
                ck.confidence, ck.updated_at,
                p.topic_key, p.canonical_text,
                ctx.claim_kind, ctx.predicate_family
            FROM aios.character_proposition_knowledge ck
            JOIN aios.character_instance ci ON ci.instance_id=ck.instance_id
            JOIN aios.proposition p ON p.proposition_id=ck.proposition_id
            LEFT JOIN LATERAL (
                SELECT ccr.claim_kind, ccr.predicate_family
                FROM aios.observation o
                JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
                WHERE o.proposition_id=ck.proposition_id
                ORDER BY ccr.resolved_at DESC
                LIMIT 1
            ) ctx ON true

            UNION ALL

            SELECT
                'world_assertion'::text AS object_type,
                ('world:' || wa.assertion_id::text) AS object_key,
                NULL::uuid AS instance_id, NULL::text AS character_id, wa.world_id,
                wa.proposition_id, wa.epistemic_status, wa.source_kind AS acquisition_mode,
                wa.confidence, wa.updated_at,
                p.topic_key, p.canonical_text,
                ctx.claim_kind, ctx.predicate_family
            FROM aios.world_proposition_assertion wa
            JOIN aios.proposition p ON p.proposition_id=wa.proposition_id
            LEFT JOIN LATERAL (
                SELECT ccr.claim_kind, ccr.predicate_family
                FROM aios.observation o
                JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
                WHERE o.proposition_id=wa.proposition_id
                ORDER BY ccr.resolved_at DESC
                LIMIT 1
            ) ctx ON true
        ) e
        WHERE e.epistemic_status NOT IN ('rejected','superseded')
          AND NOT EXISTS (
              SELECT 1 FROM aios.semantic_vector_index_state s
              WHERE s.object_type=e.object_type
                AND s.object_key=e.object_key
                AND s.qdrant_collection=$2
                AND s.embedding_model=$3
                AND s.embedding_version=$4
                AND s.indexed_at >= e.updated_at
          )
        ORDER BY e.updated_at
        LIMIT $1
        """,
        cfg.batch_size, cfg.epistemic_collection,
        cfg.embedding_model, cfg.embedding_version,
    )
    if not rows:
        return 0

    texts = [
        " | ".join(filter(None, [
            f"kind: {r['claim_kind']}" if r["claim_kind"] else None,
            f"status: {r['epistemic_status']}",
            f"topic: {r['topic_key']}",
            f"text: {r['canonical_text']}",
        ]))
        for r in rows
    ]
    hashes = [stable_text_hash(t) for t in texts]
    vectors = _get_embedder(cfg).embed(texts)
    points = []
    for row, vector, vector_hash in zip(rows, vectors, hashes):
        payload = {
            "object_type": row["object_type"],
            "object_key": row["object_key"],
            "proposition_id": str(row["proposition_id"]),
            "instance_id": str(row["instance_id"]) if row["instance_id"] else None,
            "character_id": row["character_id"],
            "world_id": str(row["world_id"]) if row["world_id"] else None,
            "epistemic_status": row["epistemic_status"],
            "acquisition_mode": row["acquisition_mode"],
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
            "topic_key": row["topic_key"],
            "claim_kind": row["claim_kind"],
            "predicate_family": row["predicate_family"],
            "created_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "embedding_version": cfg.embedding_version,
            "vector_hash": vector_hash,
        }
        points.append(qm.PointStruct(
            id=_epistemic_point_id(row["object_key"]), vector=vector,
            payload={k:v for k,v in payload.items() if v is not None},
        ))
    _get_store(cfg, cfg.epistemic_collection).upsert(points)
    for row, vector_hash in zip(rows, hashes):
        await _mark_indexed(
            db, cfg, object_type=row["object_type"], object_key=row["object_key"],
            collection=cfg.epistemic_collection, vector_hash=vector_hash,
        )
    logger.info("Indexed %d epistemic objects into Qdrant [%s]", len(rows), cfg.epistemic_collection)
    return len(rows)


async def index_once(db: Database, cfg: SemanticIndexConfig) -> int:
    source = await index_source_sections_once(db, cfg)
    propositions = await index_propositions_once(db, cfg)
    epistemic = await index_epistemic_objects_once(db, cfg)
    return source + propositions + epistemic
