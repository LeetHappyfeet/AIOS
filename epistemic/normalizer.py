from __future__ import annotations

import json
import hashlib
import re
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID

from aios_app.db import Database

NORMALIZER_VERSION = "proposition-v1"

# Different objects for these predicates are usually competing values for one
# semantic slot. Open-ended predicates are deliberately excluded.
EXCLUSIVE_PREDICATES = {
    "be",
    "be_definition_of",
    "located_at",
    "location",
    "born_in",
    "works_for",
    "employed_by",
    "parent_of",
    "spouse_of",
    "identity",
    "status",
}


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value.strip().lower())
    value = value.strip(" .,:;!?\t\n\r")
    return value or None


def _detect_polarity(raw_text: str) -> int:
    text = f" {_clean(raw_text) or ''} "
    negative_markers = (" not ", " never ", " no longer ", "n't ")
    return -1 if any(marker in text for marker in negative_markers) else 1


def normalize_components(
    *,
    subject: Optional[str],
    predicate: Optional[str],
    object_value: Optional[str],
    raw_text: str,
    polarity: Optional[int] = None,
) -> dict[str, Any]:
    subject_norm = _clean(subject)
    predicate_norm = _clean(predicate)
    object_norm = _clean(object_value)
    polarity = polarity if polarity in (-1, 1) else _detect_polarity(raw_text)

    if subject_norm or predicate_norm or object_norm:
        canonical_text = " | ".join(
            part or "_"
            for part in (subject_norm, predicate_norm, object_norm)
        )
        canonical_text = ("NOT " if polarity < 0 else "") + canonical_text
    else:
        canonical_text = _clean(raw_text) or raw_text.strip()

    identity_material = "\x1f".join(
        [
            subject_norm or "",
            predicate_norm or "",
            object_norm or "",
            str(polarity),
            canonical_text,
        ]
    )
    proposition_hash = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()

    topic_material = "\x1f".join(
        [
            subject_norm or "",
            predicate_norm or "",
        ]
    )
    if not subject_norm and not predicate_norm:
        # Unresolved natural-language claims remain separate topics until a
        # later semantic resolver can safely identify their subject/predicate.
        topic_material = canonical_text
    topic_key = hashlib.sha256(topic_material.encode("utf-8")).hexdigest()

    return {
        "proposition_hash": proposition_hash,
        "topic_key": topic_key,
        "subject_norm": subject_norm,
        "predicate_norm": predicate_norm,
        "object_norm": object_norm,
        "polarity": polarity,
        "canonical_text": canonical_text,
    }


async def ensure_proposition(
    db: Database,
    *,
    subject: Optional[str],
    predicate: Optional[str],
    object_value: Optional[str],
    raw_text: str,
    polarity: Optional[int] = None,
    modality: str = "asserted",
    meta: Optional[dict] = None,
) -> UUID:
    norm = normalize_components(
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        raw_text=raw_text,
        polarity=polarity,
    )
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.proposition (
            proposition_hash, topic_key, subject_norm, predicate_norm,
            object_norm, polarity, canonical_text, modality, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
        ON CONFLICT (proposition_hash) DO UPDATE
        SET proposition_hash=EXCLUDED.proposition_hash
        RETURNING proposition_id
        """,
        norm["proposition_hash"],
        norm["topic_key"],
        norm["subject_norm"],
        norm["predicate_norm"],
        norm["object_norm"],
        norm["polarity"],
        norm["canonical_text"],
        modality,
        json.dumps(meta or {}),
    )
    return row["proposition_id"]


async def normalize_claim_once(db: Database, *, claim_id: UUID) -> UUID:
    """Convert one immutable claim observation into a normalized proposition."""
    existing = await db.fetchrow(
        "SELECT proposition_id FROM aios.observation WHERE claim_id=$1",
        claim_id,
    )
    if existing:
        return existing["proposition_id"]

    row = await db.fetchrow(
        """
        SELECT
            cc.claim_id, cc.subject, cc.predicate, cc.object, cc.raw_text,
            cc.confidence, cc.extraction_ver, cc.created_at,
            ds.document_id, n.node_id, n.timeline_id,
            n.speaker_id, n.speaker_role::text AS speaker_role, n.recipient_id,
            ie.source AS ingest_source,
            sd.source_type, sd.source_url, sd.retrieved_at
        FROM aios.claim_candidate cc
        LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
        LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
        LEFT JOIN aios.dag_node n ON n.node_id=ds.node_id
        LEFT JOIN aios.ingest_event ie ON ie.event_id=n.event_id
        LEFT JOIN aios.source_document sd ON sd.document_id=ds.document_id
        WHERE cc.claim_id=$1
        """,
        claim_id,
    )
    if not row:
        raise RuntimeError(f"Cannot normalize missing claim {claim_id}")

    proposition_id = await ensure_proposition(
        db,
        subject=row["subject"],
        predicate=row["predicate"],
        object_value=row["object"],
        raw_text=row["raw_text"],
        meta={
            "normalizer_version": NORMALIZER_VERSION,
            "extraction_version": row["extraction_ver"],
        },
    )

    source_domain = None
    if row["source_url"]:
        try:
            source_domain = urlparse(row["source_url"]).netloc.lower() or None
        except ValueError:
            source_domain = None

    source_key = source_domain or row["ingest_source"] or row["source_type"] or "unknown"

    if row["source_type"]:
        source_kind = row["source_type"]
    elif row["ingest_source"]:
        source_kind = (
            "internet"
            if str(row["ingest_source"]).lower() in {"internet", "web", "accumulator"}
            else "chat"
        )
    else:
        source_kind = "unknown"

    lineage_complete = bool(
        row["node_id"] is not None
        and row["timeline_id"] is not None
        and row["ingest_source"] is not None
    )

    observation = await db.execute_returning_row(
        """
        INSERT INTO aios.observation (
            claim_id, proposition_id, document_id, timeline_id, dag_node_id,
            source_key, source_domain, source_kind, observed_at,
            extraction_confidence, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,
                COALESCE($9::timestamptz,$10::timestamptz,now()),$11,$12::jsonb)
        ON CONFLICT (claim_id) DO UPDATE
        SET proposition_id=EXCLUDED.proposition_id
        RETURNING observation_id
        """,
        claim_id,
        proposition_id,
        row["document_id"],
        row["timeline_id"],
        row["node_id"],
        source_key,
        source_domain,
        source_kind,
        row["retrieved_at"],
        row["created_at"],
        float(row["confidence"] or 0.0),
        json.dumps({
            "normalizer_version": NORMALIZER_VERSION,
            "lineage_complete": lineage_complete,
            "legacy_partial_lineage": not lineage_complete,
            "viewpoint_id": row["speaker_id"],
            "speaker_role": row["speaker_role"],
            "recipient_id": row["recipient_id"],
            "epistemic_scope": (
                "character"
                if row["speaker_role"] in {"character", "agent"} and row["speaker_id"]
                else "speaker"
                if row["speaker_id"]
                else "source"
            ),
            "semantic_pivot_resolved": "pivot-v1" in (row["extraction_rule"] or ""),
        }),
    )

    provenance = await db.fetchrow(
        """
        SELECT source_weight
        FROM aios.claim_provenance
        WHERE claim_id=$1
        ORDER BY source_weight DESC
        LIMIT 1
        """,
        claim_id,
    )
    source_weight = float(provenance["source_weight"]) if provenance else 0.5

    await db.execute(
        """
        INSERT INTO aios.proposition_evidence (
            proposition_id, observation_id, evidence_role,
            source_weight, confidence, meta
        )
        VALUES ($1,$2,'support',$3,$4,$5::jsonb)
        ON CONFLICT (proposition_id, observation_id, evidence_role) DO NOTHING
        """,
        proposition_id,
        observation["observation_id"],
        source_weight,
        float(row["confidence"] or 0.0),
        json.dumps({"claim_id": str(claim_id)}),
    )

    await _detect_conflicts(db, proposition_id=proposition_id)
    return proposition_id


async def _detect_conflicts(db: Database, *, proposition_id: UUID) -> int:
    current = await db.fetchrow(
        """
        SELECT proposition_id, topic_key, predicate_norm, object_norm, polarity
        FROM aios.proposition
        WHERE proposition_id=$1
        """,
        proposition_id,
    )
    if not current:
        return 0

    peers = await db.fetch(
        """
        SELECT proposition_id, predicate_norm, object_norm, polarity
        FROM aios.proposition
        WHERE topic_key=$1 AND proposition_id<>$2
        """,
        current["topic_key"],
        proposition_id,
    )

    inserted = 0
    for peer in peers:
        conflict_type = None
        strength = 1.0

        if int(peer["polarity"]) != int(current["polarity"]):
            conflict_type = "opposite_polarity"
        elif (
            current["predicate_norm"] in EXCLUSIVE_PREDICATES
            and current["object_norm"]
            and peer["object_norm"]
            and current["object_norm"] != peer["object_norm"]
        ):
            conflict_type = "exclusive_object"
            strength = 0.85

        if not conflict_type:
            continue

        result = await db.execute(
            """
            INSERT INTO aios.proposition_conflict (
                topic_key, proposition_a_id, proposition_b_id,
                conflict_type, strength, meta
            )
            VALUES ($1,$2,$3,$4,$5,$6::jsonb)
            ON CONFLICT DO NOTHING
            """,
            current["topic_key"],
            proposition_id,
            peer["proposition_id"],
            conflict_type,
            strength,
            json.dumps({"detector": NORMALIZER_VERSION}),
        )
        if result.endswith("1"):
            inserted += 1

    return inserted
