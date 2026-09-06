from __future__ import annotations

from typing import Optional
from uuid import UUID

from aios_app.db import Database


async def _resolve_instance_scope(
    db: Database,
    *,
    character_id: Optional[str],
    instance_id: Optional[UUID],
) -> tuple[Optional[str], Optional[UUID]]:
    if instance_id is not None:
        row = await db.fetchrow(
            "SELECT character_id FROM aios.character_instance WHERE instance_id=$1",
            instance_id,
        )
        if not row:
            raise ValueError(f"unknown character instance {instance_id}")
        if character_id is not None and row["character_id"] != character_id:
            raise ValueError(
                f"instance {instance_id} belongs to character {row['character_id']}, not {character_id}"
            )
        return row["character_id"], instance_id

    return character_id, None


async def epistemic_search(
    db: Database,
    *,
    query: str,
    limit: int = 25,
    character_id: Optional[str] = None,
    instance_id: Optional[UUID] = None,
    source_key: Optional[str] = None,
    include_conflicts: bool = True,
) -> dict:
    q = query.strip()
    if not q:
        return {"query": query, "results": []}

    resolved_character_id, resolved_instance_id = await _resolve_instance_scope(
        db,
        character_id=character_id,
        instance_id=instance_id,
    )
    rows = await db.fetch(
        """
        WITH matched AS (
            SELECT DISTINCT p.proposition_id
            FROM aios.proposition p
            LEFT JOIN aios.observation o ON o.proposition_id=p.proposition_id
            LEFT JOIN aios.claim_candidate cc ON cc.claim_id=o.claim_id
            WHERE (
                p.canonical_text ILIKE '%' || $1 || '%'
                OR COALESCE(p.subject_norm,'') ILIKE '%' || $1 || '%'
                OR COALESCE(p.predicate_norm,'') ILIKE '%' || $1 || '%'
                OR COALESCE(p.object_norm,'') ILIKE '%' || $1 || '%'
                OR COALESCE(cc.raw_text,'') ILIKE '%' || $1 || '%'
            )
            AND ($2::text IS NULL OR o.source_key=$2)
            AND (
                (
                    $4::uuid IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM aios.character_proposition_knowledge known
                        WHERE known.instance_id=$4
                          AND known.proposition_id=p.proposition_id
                    )
                )
                OR (
                    $4::uuid IS NULL
                    AND $5::text IS NOT NULL
                    AND COALESCE(
                        o.meta->>'memory_owner_id',
                        o.meta->>'character_id'
                    )=$5
                )
                OR ($4::uuid IS NULL AND $5::text IS NULL)
            )
            LIMIT $3
        )
        SELECT
            p.proposition_id, p.topic_key, p.canonical_text,
            p.subject_norm, p.predicate_norm, p.object_norm,
            p.polarity, p.modality,
            count(DISTINCT o.observation_id) AS observation_count,
            count(DISTINCT o.source_key) AS source_count,
            max(cpk.effective_confidence) AS character_effective_confidence,
            max(cpk.epistemic_status) AS character_epistemic_status
        FROM matched m
        JOIN aios.proposition p ON p.proposition_id=m.proposition_id
        LEFT JOIN aios.observation o ON o.proposition_id=p.proposition_id
        LEFT JOIN aios.character_proposition_knowledge cpk
          ON cpk.proposition_id=p.proposition_id
         AND cpk.instance_id=$4
        GROUP BY p.proposition_id
        ORDER BY
            character_effective_confidence DESC NULLS LAST,
            observation_count DESC,
            source_count DESC,
            p.canonical_text
        LIMIT $3
        """,
        q,
        source_key,
        limit,
        resolved_instance_id,
        resolved_character_id,
    )

    results = []
    for row in rows:
        item = dict(row)
        if include_conflicts:
            conflicts = await db.fetch(
                """
                SELECT
                    pc.conflict_type, pc.strength,
                    CASE WHEN pc.proposition_a_id=$1
                         THEN pc.proposition_b_id ELSE pc.proposition_a_id END
                         AS competing_proposition_id,
                    cp.canonical_text AS competing_text
                FROM aios.proposition_conflict pc
                JOIN aios.proposition cp
                  ON cp.proposition_id = CASE WHEN pc.proposition_a_id=$1
                       THEN pc.proposition_b_id ELSE pc.proposition_a_id END
                WHERE (pc.proposition_a_id=$1 OR pc.proposition_b_id=$1)
                  AND (
                      (
                          $2::uuid IS NOT NULL
                          AND EXISTS (
                              SELECT 1
                              FROM aios.character_proposition_knowledge known
                              WHERE known.instance_id=$2
                                AND known.proposition_id = CASE
                                    WHEN pc.proposition_a_id=$1
                                    THEN pc.proposition_b_id
                                    ELSE pc.proposition_a_id
                                END
                          )
                      )
                      OR (
                          $2::uuid IS NULL
                          AND $3::text IS NOT NULL
                          AND EXISTS (
                              SELECT 1
                              FROM aios.observation owned
                              WHERE owned.proposition_id = CASE
                                  WHEN pc.proposition_a_id=$1
                                  THEN pc.proposition_b_id
                                  ELSE pc.proposition_a_id
                              END
                                AND COALESCE(
                                    owned.meta->>'memory_owner_id',
                                    owned.meta->>'character_id'
                                )=$3
                          )
                      )
                      OR ($2::uuid IS NULL AND $3::text IS NULL)
                  )
                ORDER BY pc.strength DESC
                """,
                row["proposition_id"],
                resolved_instance_id,
                resolved_character_id,
            )
            item["conflicts"] = [dict(c) for c in conflicts]
        results.append(item)

    return {
        "query": query,
        "character_id": resolved_character_id,
        "instance_id": resolved_instance_id,
        "source_key": source_key,
        "epistemic_scope": (
            "character_instance"
            if resolved_instance_id
            else "character"
            if resolved_character_id
            else "global_observation"
        ),
        "results": results,
    }


async def document_epistemic_summary(db: Database, *, document_id: UUID) -> dict:
    doc = await db.fetchrow(
        """
        SELECT document_id, source_type, source_url, title, retrieved_at, meta
        FROM aios.source_document WHERE document_id=$1
        """,
        document_id,
    )
    if not doc:
        raise ValueError(f"unknown document {document_id}")

    metadata = await db.fetch(
        """
        SELECT field_type, raw_value, normalized_value, source_location,
               confidence, extraction_method
        FROM aios.document_metadata_observation
        WHERE document_id=$1
        ORDER BY field_type, confidence DESC
        """,
        document_id,
    )
    structure = await db.fetch(
        """
        SELECT unit_id, parent_unit_id, unit_type, unit_index, path, title,
               depth, start_char, end_char
        FROM aios.document_unit
        WHERE document_id=$1
        ORDER BY unit_index, path
        """,
        document_id,
    )
    propositions = await db.fetch(
        """
        SELECT p.proposition_id, p.canonical_text, p.topic_key,
               count(DISTINCT o.observation_id) AS observation_count
        FROM aios.observation o
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        WHERE o.document_id=$1
        GROUP BY p.proposition_id
        ORDER BY observation_count DESC, p.canonical_text
        LIMIT 500
        """,
        document_id,
    )
    return {
        "document": dict(doc),
        "metadata_observations": [dict(x) for x in metadata],
        "structure": [dict(x) for x in structure],
        "propositions": [dict(x) for x in propositions],
    }
