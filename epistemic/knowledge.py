from __future__ import annotations

import json
import re
from typing import Optional
from uuid import UUID

from aios_app.db import Database
from .weights import calculate_weights


_CONTEXT_KNOWLEDGE_KINDS = {
    "BELIEF",
    "MEMORY",
    "GOAL",
    "RULE",
    "STATE",
    "TRAIT",
    "RELATIONSHIP",
}
_NONASSERTIVE_DISCOURSE = {
    "question",
    "hypothetical",
    "counterfactual",
    "conditional",
    "quoted_question",
}
_QUESTION_END_RE = re.compile(r"\?\s*[\"'”’\)\]]*\s*$")


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


def _context_generated(meta: object) -> bool:
    source = str(_json_object(meta).get("source") or "").strip().lower()
    return source.startswith("context-resolver")


def _context_acquisition_eligible_values(
    *,
    instance_id: UUID | str,
    epistemic_scope: Optional[str],
    character_instance_id: Optional[UUID | str],
    claim_kind: Optional[str],
    raw_text: Optional[str],
    discourse_mode: Optional[str],
) -> bool:
    """Return whether resolved context is eligible for settled /char knowledge.

    Context-generated acquisitions are deliberately narrower than generic
    knowledge acquisitions.  Encountering a proposition is not the same as
    currently accepting it.  Narrative/speaker/source claims, event fragments,
    questions and explicitly non-assertive discourse stay in evidence/episode
    topology rather than being promoted to settled character knowledge.
    """
    if (epistemic_scope or "").lower() != "character":
        return False
    if character_instance_id is None or str(character_instance_id) != str(instance_id):
        return False
    if (claim_kind or "").upper() not in _CONTEXT_KNOWLEDGE_KINDS:
        return False
    if (discourse_mode or "").lower() in _NONASSERTIVE_DISCOURSE:
        return False
    if raw_text and _QUESTION_END_RE.search(raw_text.strip()):
        return False
    return True


async def _context_row(db: Database, *, claim_id: UUID):
    return await db.fetchrow(
        """
        SELECT
            ccr.epistemic_scope,
            ccr.character_instance_id,
            ccr.claim_kind,
            cc.raw_text,
            sf.discourse_mode,
            sf.modality
        FROM aios.claim_candidate cc
        LEFT JOIN aios.claim_context_resolution ccr
          ON ccr.claim_id=cc.claim_id
        LEFT JOIN aios.claim_semantic_frame_projection sfp
          ON sfp.claim_id=cc.claim_id
        LEFT JOIN aios.claim_semantic_frame sf
          ON sf.frame_id=sfp.primary_frame_id
        WHERE cc.claim_id=$1
        """,
        claim_id,
    )


async def _context_acquisition_eligible(db: Database, row) -> bool:
    if row["claim_id"] is None or not _context_generated(row["meta"]):
        return True
    context = await _context_row(db, claim_id=row["claim_id"])
    if not context:
        return False
    return _context_acquisition_eligible_values(
        instance_id=row["instance_id"],
        epistemic_scope=context["epistemic_scope"],
        character_instance_id=context["character_instance_id"],
        claim_kind=context["claim_kind"],
        raw_text=context["raw_text"],
        discourse_mode=context["discourse_mode"],
    )


async def _retract_acquisition_row(db: Database, row) -> None:
    """Retract one stale context-generated acquisition without touching other evidence."""
    acquisition_id = row["acquisition_id"]
    instance_id = row["instance_id"]
    claim_id = row["claim_id"]
    proposition_id = row["proposition_id"]
    if proposition_id is None and claim_id is not None:
        obs = await db.fetchrow(
            "SELECT proposition_id FROM aios.observation WHERE claim_id=$1",
            claim_id,
        )
        proposition_id = obs["proposition_id"] if obs else None

    other_claim_support = None
    if claim_id is not None:
        other_claim_support = await db.fetchrow(
            """
            SELECT 1
            FROM aios.knowledge_acquisition_event kae
            WHERE kae.instance_id=$1
              AND kae.claim_id=$2
              AND kae.acquisition_id<>$3
            LIMIT 1
            """,
            instance_id,
            claim_id,
            acquisition_id,
        )

    other_prop_support = None
    if proposition_id is not None:
        other_prop_support = await db.fetchrow(
            """
            SELECT 1
            FROM aios.knowledge_acquisition_event kae
            LEFT JOIN aios.observation o ON o.claim_id=kae.claim_id
            WHERE kae.instance_id=$1
              AND kae.acquisition_id<>$2
              AND COALESCE(kae.proposition_id, o.proposition_id)=$3
            LIMIT 1
            """,
            instance_id,
            acquisition_id,
            proposition_id,
        )

    await db.execute(
        "DELETE FROM aios.knowledge_acquisition_event WHERE acquisition_id=$1",
        acquisition_id,
    )

    if claim_id is not None and not other_claim_support:
        await db.execute(
            """
            DELETE FROM aios.character_knowledge
            WHERE instance_id=$1
              AND claim_id=$2
              AND COALESCE((meta->>'copied_on_fork')::boolean, false)=false
            """,
            instance_id,
            claim_id,
        )

    if proposition_id is not None and not other_prop_support:
        await db.execute(
            """
            DELETE FROM aios.character_proposition_knowledge
            WHERE instance_id=$1
              AND proposition_id=$2
              AND COALESCE((meta->>'copied_on_fork')::boolean, false)=false
            """,
            instance_id,
            proposition_id,
        )


async def reconcile_context_acquisitions_for_claim(
    db: Database,
    *,
    claim_id: UUID,
) -> int:
    """Remove context-generated /char acquisitions no longer justified by context."""
    rows = await db.fetch(
        """
        SELECT *
        FROM aios.knowledge_acquisition_event
        WHERE claim_id=$1
          AND lower(COALESCE(meta->>'source','')) LIKE 'context-resolver%'
        ORDER BY created_at
        """,
        claim_id,
    )
    retracted = 0
    for row in rows:
        if await _context_acquisition_eligible(db, row):
            continue
        await _retract_acquisition_row(db, row)
        retracted += 1
    return retracted


async def reconcile_stale_context_acquisitions(
    db: Database,
    *,
    instance_id: Optional[UUID] = None,
    limit: int = 500,
) -> int:
    """Bounded repair pass for acquisitions created before the current context receipt."""
    rows = await db.fetch(
        """
        SELECT DISTINCT claim_id
        FROM aios.knowledge_acquisition_event
        WHERE claim_id IS NOT NULL
          AND lower(COALESCE(meta->>'source','')) LIKE 'context-resolver%'
          AND ($2::uuid IS NULL OR instance_id=$2)
        ORDER BY claim_id
        LIMIT $1
        """,
        limit,
        instance_id,
    )
    retracted = 0
    for row in rows:
        retracted += await reconcile_context_acquisitions_for_claim(
            db,
            claim_id=row["claim_id"],
        )
    return retracted


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
    # Re-resolution is allowed to change epistemic ownership. Clean old
    # context-generated acquisitions before materializing anything new so the
    # /char plane reflects current belief eligibility rather than historical
    # evidence routing.
    await reconcile_stale_context_acquisitions(
        db,
        instance_id=instance_id,
        limit=max(limit, 500),
    )

    rows = await db.fetch(
        """
        SELECT *
        FROM aios.knowledge_acquisition_event
        WHERE processed_at IS NULL
          AND ($2::uuid IS NULL OR instance_id=$2)
          AND (
              claim_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM aios.claim_candidate cc
                  JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
                  JOIN aios.document_section ds ON ds.section_id=es.section_id
                  JOIN aios.dag_node dn ON dn.node_id=ds.node_id
                  JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
                  WHERE cc.claim_id=aios.knowledge_acquisition_event.claim_id
                    AND ie.superseded_at IS NULL
              )
          )
        ORDER BY created_at
        LIMIT $1
        """,
        limit,
        instance_id,
    )
    projected = 0

    for row in rows:
        if not await _context_acquisition_eligible(db, row):
            await _retract_acquisition_row(db, row)
            continue

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
                json.dumps({
                    "acquisition_mode": row["acquisition_mode"],
                    "acquisition_source": acquisition_meta.get("source"),
                }),
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
