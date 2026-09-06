from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from aios_app.db import Database
from .weights import calculate_weights


def _json_object(value: object) -> dict:
    """Normalize asyncpg JSON/JSONB results to a Python mapping."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if decoded is None:
            return {}
        if not isinstance(decoded, dict):
            raise ValueError("expected JSON object metadata")
        return decoded
    try:
        return dict(value)  # asyncpg codecs/custom mappings may already be mapping-like
    except (TypeError, ValueError) as exc:
        raise ValueError("expected object-like metadata") from exc


async def record_acquisition(
    db: Database,
    *,
    instance_id: UUID,
    acquisition_mode: str,
    proposition_id: Optional[UUID] = None,
    claim_id: Optional[UUID] = None,
    epistemic_status: str = "observed",
    confidence: Optional[float] = None,
    source_entity_id: Optional[UUID] = None,
    dag_node_id: Optional[UUID] = None,
    meta: Optional[dict] = None,
) -> UUID:
    if proposition_id is None and claim_id is None:
        raise ValueError("proposition_id or claim_id is required")

    if proposition_id is None and claim_id is not None:
        claim_exists = await db.fetchrow(
            "SELECT 1 FROM aios.claim_candidate WHERE claim_id=$1",
            claim_id,
        )
        if not claim_exists:
            raise ValueError(f"unknown claim {claim_id}")
        row = await db.fetchrow(
            "SELECT proposition_id FROM aios.observation WHERE claim_id=$1",
            claim_id,
        )
        proposition_id = row["proposition_id"] if row else None

    exists = await db.fetchrow(
        "SELECT 1 FROM aios.character_instance WHERE instance_id=$1",
        instance_id,
    )
    if not exists:
        raise ValueError(f"unknown character instance {instance_id}")

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.knowledge_acquisition_event (
            instance_id, proposition_id, claim_id, acquisition_mode,
            epistemic_status, confidence, source_entity_id, dag_node_id, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
        RETURNING acquisition_id
        """,
        instance_id,
        proposition_id,
        claim_id,
        acquisition_mode,
        epistemic_status,
        confidence,
        source_entity_id,
        dag_node_id,
        json.dumps(meta or {}),
    )
    return row["acquisition_id"]


async def project_knowledge_acquisitions_once(
    db: Database,
    *,
    limit: int = 200,
    instance_id: Optional[UUID] = None,
) -> int:
    rows = await db.fetch(
        """
        SELECT *
        FROM aios.knowledge_acquisition_event
        WHERE processed_at IS NULL
          AND ($2::uuid IS NULL OR instance_id=$2)
        ORDER BY created_at
        LIMIT $1
        """,
        limit,
        instance_id,
    )
    projected = 0

    for row in rows:
        proposition_id = row["proposition_id"]
        if proposition_id is None and row["claim_id"] is not None:
            obs = await db.fetchrow(
                "SELECT proposition_id FROM aios.observation WHERE claim_id=$1",
                row["claim_id"],
            )
            if not obs:
                continue
            proposition_id = obs["proposition_id"]

        acquisition_meta = _json_object(row["meta"])
        source = await db.fetchrow(
            """
            SELECT o.source_key
            FROM aios.observation o
            WHERE o.proposition_id=$1
            ORDER BY o.observed_at DESC
            LIMIT 1
            """,
            proposition_id,
        )
        weights = await calculate_weights(
            db,
            instance_id=row["instance_id"],
            proposition_id=proposition_id,
            acquisition_mode=row["acquisition_mode"],
            base_confidence=row["confidence"],
            source_key=acquisition_meta.get("source_key") or (source["source_key"] if source else None),
        )

        await db.execute(
            """
            INSERT INTO aios.character_proposition_knowledge (
                instance_id, proposition_id, epistemic_status, confidence,
                acquisition_mode, source_entity_id, first_node_id, last_node_id,
                first_acquired_at, updated_at, meta,
                base_confidence, attention_weight, trust_weight,
                compatibility_weight, retention_weight, salience_weight,
                effective_confidence
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$7,$8,$8,$9::jsonb,
                    $10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (instance_id, proposition_id) DO UPDATE
            SET epistemic_status=EXCLUDED.epistemic_status,
                confidence=COALESCE(EXCLUDED.confidence,
                                    aios.character_proposition_knowledge.confidence),
                acquisition_mode=EXCLUDED.acquisition_mode,
                source_entity_id=COALESCE(EXCLUDED.source_entity_id,
                                          aios.character_proposition_knowledge.source_entity_id),
                last_node_id=COALESCE(EXCLUDED.last_node_id,
                                      aios.character_proposition_knowledge.last_node_id),
                updated_at=EXCLUDED.updated_at,
                meta=aios.character_proposition_knowledge.meta || EXCLUDED.meta,
                base_confidence=EXCLUDED.base_confidence,
                attention_weight=EXCLUDED.attention_weight,
                trust_weight=EXCLUDED.trust_weight,
                compatibility_weight=EXCLUDED.compatibility_weight,
                retention_weight=EXCLUDED.retention_weight,
                salience_weight=EXCLUDED.salience_weight,
                effective_confidence=EXCLUDED.effective_confidence
            """,
            row["instance_id"],
            proposition_id,
            row["epistemic_status"],
            row["confidence"],
            row["acquisition_mode"],
            row["source_entity_id"],
            row["dag_node_id"],
            row["created_at"],
            json.dumps({
                **acquisition_meta,
                "weight_profile_character_id": weights["profile_character_id"],
            }),
            weights["base_confidence"],
            weights["attention_weight"],
            weights["trust_weight"],
            weights["compatibility_weight"],
            weights["retention_weight"],
            weights["salience_weight"],
            weights["effective_confidence"],
        )

        # Keep the legacy claim-level projection populated when a concrete
        # source claim exists. New prompt construction should prefer the
        # proposition-level table.
        if row["claim_id"] is not None:
            await db.execute(
                """
                INSERT INTO aios.character_knowledge (
                    instance_id, claim_id, epistemic_status, confidence,
                    source_entity_id, first_node_id, last_node_id, meta
                )
                VALUES ($1,$2,$3,$4,$5,$6,$6,$7::jsonb)
                ON CONFLICT (instance_id, claim_id) DO UPDATE
                SET epistemic_status=EXCLUDED.epistemic_status,
                    confidence=COALESCE(EXCLUDED.confidence,
                                        aios.character_knowledge.confidence),
                    last_node_id=COALESCE(EXCLUDED.last_node_id,
                                          aios.character_knowledge.last_node_id),
                    updated_at=now(),
                    meta=aios.character_knowledge.meta || EXCLUDED.meta
                """,
                row["instance_id"],
                row["claim_id"],
                row["epistemic_status"],
                row["confidence"],
                row["source_entity_id"],
                row["dag_node_id"],
                json.dumps({"acquisition_mode": row["acquisition_mode"]}),
            )

        await db.execute(
            """
            UPDATE aios.knowledge_acquisition_event
            SET proposition_id=$2, processed_at=now()
            WHERE acquisition_id=$1
            """,
            row["acquisition_id"],
            proposition_id,
        )
        projected += 1

    return projected


async def acquire_document(
    db: Database,
    *,
    instance_id: UUID,
    document_id: UUID,
    acquisition_mode: str = "read_document",
    epistemic_status: str = "observed",
    confidence: Optional[float] = None,
) -> dict:
    """Queue every normalized proposition observed in a document for one character."""
    exists = await db.fetchrow(
        "SELECT 1 FROM aios.source_document WHERE document_id=$1",
        document_id,
    )
    if not exists:
        raise ValueError(f"unknown document {document_id}")

    observations = await db.fetch(
        """
        SELECT DISTINCT ON (o.proposition_id)
            o.proposition_id, o.claim_id, o.dag_node_id, o.extraction_confidence,
            o.source_key
        FROM aios.observation o
        WHERE o.document_id=$1
        ORDER BY o.proposition_id, o.observed_at
        """,
        document_id,
    )

    queued = 0
    for obs in observations:
        base = confidence
        if base is None:
            raw = float(obs["extraction_confidence"] or 0.0)
            base = raw if raw > 0 else 0.5

        await record_acquisition(
            db,
            instance_id=instance_id,
            proposition_id=obs["proposition_id"],
            claim_id=obs["claim_id"],
            acquisition_mode=acquisition_mode,
            epistemic_status=epistemic_status,
            confidence=base,
            dag_node_id=obs["dag_node_id"],
            meta={
                "document_id": str(document_id),
                "source_key": obs["source_key"],
            },
        )
        queued += 1

    return {
        "instance_id": instance_id,
        "document_id": document_id,
        "queued_propositions": queued,
        "acquisition_mode": acquisition_mode,
    }
