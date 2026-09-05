from __future__ import annotations

from typing import Optional
from uuid import UUID

from aios_app.db import Database


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

    if proposition_id is None:
        row = await db.fetchrow(
            "SELECT proposition_id FROM aios.observation WHERE claim_id=$1",
            claim_id,
        )
        if not row:
            raise ValueError("claim has not been normalized into a proposition yet")
        proposition_id = row["proposition_id"]

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
        meta or {},
    )
    return row["acquisition_id"]


async def project_knowledge_acquisitions_once(
    db: Database,
    *,
    limit: int = 200,
) -> int:
    rows = await db.fetch(
        """
        SELECT *
        FROM aios.knowledge_acquisition_event
        WHERE processed_at IS NULL
        ORDER BY created_at
        LIMIT $1
        """,
        limit,
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

        await db.execute(
            """
            INSERT INTO aios.character_proposition_knowledge (
                instance_id, proposition_id, epistemic_status, confidence,
                acquisition_mode, source_entity_id, first_node_id, last_node_id,
                first_acquired_at, updated_at, meta
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$7,$8,$8,$9::jsonb)
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
                meta=aios.character_proposition_knowledge.meta || EXCLUDED.meta
            """,
            row["instance_id"],
            proposition_id,
            row["epistemic_status"],
            row["confidence"],
            row["acquisition_mode"],
            row["source_entity_id"],
            row["dag_node_id"],
            row["created_at"],
            row["meta"] or {},
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
                {"acquisition_mode": row["acquisition_mode"]},
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
