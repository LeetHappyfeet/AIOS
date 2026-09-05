from __future__ import annotations

import json
from math import prod
from typing import Optional
from uuid import UUID

from aios_app.db import Database

DEFAULT_PROFILE = {
    "skepticism": 0.5,
    "curiosity": 0.5,
    "authority_trust": 0.5,
    "novelty_seeking": 0.5,
    "emotional_reactivity": 0.5,
    "retention": 0.7,
    "source_trust": {},
    "topic_interest": {},
    "domain_expertise": {},
    "trait_weights": {},
}


def _clamp(value: float, low: float = 0.05, high: float = 1.5) -> float:
    return max(low, min(high, value))


async def get_profile(db: Database, *, character_id: str) -> dict:
    row = await db.fetchrow(
        "SELECT * FROM aios.character_epistemic_profile WHERE character_id=$1",
        character_id,
    )
    if row:
        return dict(row)

    ident = await db.fetchrow(
        """
        SELECT archetype, default_tone, primary_role, meta
        FROM aios.character_identity
        WHERE character_id=$1
        """,
        character_id,
    )
    if not ident:
        raise ValueError(f"unknown character {character_id}")

    profile = dict(DEFAULT_PROFILE)
    meta = dict(ident["meta"] or {})
    embedded = meta.get("epistemic_profile")
    if isinstance(embedded, dict):
        profile.update({k: v for k, v in embedded.items() if k in profile})

    tones = set(ident["default_tone"] or [])
    if "curious" in tones:
        profile["curiosity"] = max(float(profile["curiosity"]), 0.75)
    if "formal" in tones:
        profile["retention"] = max(float(profile["retention"]), 0.75)
    if "aggressive" in tones:
        profile["emotional_reactivity"] = max(float(profile["emotional_reactivity"]), 0.7)

    return profile


async def upsert_profile(db: Database, *, character_id: str, data: dict) -> dict:
    values = {**DEFAULT_PROFILE, **data}
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.character_epistemic_profile (
            character_id, skepticism, curiosity, authority_trust,
            novelty_seeking, emotional_reactivity, retention,
            source_trust, topic_interest, domain_expertise, trait_weights
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11::jsonb)
        ON CONFLICT (character_id) DO UPDATE
        SET skepticism=EXCLUDED.skepticism,
            curiosity=EXCLUDED.curiosity,
            authority_trust=EXCLUDED.authority_trust,
            novelty_seeking=EXCLUDED.novelty_seeking,
            emotional_reactivity=EXCLUDED.emotional_reactivity,
            retention=EXCLUDED.retention,
            source_trust=EXCLUDED.source_trust,
            topic_interest=EXCLUDED.topic_interest,
            domain_expertise=EXCLUDED.domain_expertise,
            trait_weights=EXCLUDED.trait_weights,
            updated_at=now()
        RETURNING *
        """,
        character_id,
        float(values["skepticism"]),
        float(values["curiosity"]),
        float(values["authority_trust"]),
        float(values["novelty_seeking"]),
        float(values["emotional_reactivity"]),
        float(values["retention"]),
        json.dumps(values["source_trust"] or {}),
        json.dumps(values["topic_interest"] or {}),
        json.dumps(values["domain_expertise"] or {}),
        json.dumps(values["trait_weights"] or {}),
    )
    return dict(row)


async def calculate_weights(
    db: Database,
    *,
    instance_id: UUID,
    proposition_id: UUID,
    acquisition_mode: str,
    base_confidence: Optional[float],
    source_key: Optional[str] = None,
) -> dict:
    row = await db.fetchrow(
        """
        SELECT ci.character_id, p.topic_key, p.subject_norm,
               p.predicate_norm, p.object_norm, p.modality
        FROM aios.character_instance ci
        CROSS JOIN aios.proposition p
        WHERE ci.instance_id=$1 AND p.proposition_id=$2
        """,
        instance_id,
        proposition_id,
    )
    if not row:
        raise ValueError("instance or proposition not found")

    profile = await get_profile(db, character_id=row["character_id"])

    base = float(base_confidence if base_confidence is not None else 0.5)
    curiosity = float(profile["curiosity"])
    skepticism = float(profile["skepticism"])
    retention = float(profile["retention"])
    authority_trust = float(profile["authority_trust"])

    source_trust_map = dict(profile.get("source_trust") or {})
    topic_interest_map = dict(profile.get("topic_interest") or {})
    expertise_map = dict(profile.get("domain_expertise") or {})

    source_trust = float(source_trust_map.get(source_key, 0.5)) if source_key else 0.5
    topic_interest = float(topic_interest_map.get(row["topic_key"], 0.5))
    expertise = float(
        expertise_map.get(row["predicate_norm"], expertise_map.get(row["subject_norm"], 0.5))
    )

    attention = _clamp(0.45 + 0.35 * curiosity + 0.20 * topic_interest)
    trust = _clamp(
        0.35
        + 0.35 * source_trust
        + 0.20 * authority_trust
        + 0.20 * expertise
        - 0.25 * skepticism
    )
    compatibility = _clamp(0.75 + 0.25 * expertise - 0.15 * skepticism)
    retention_weight = _clamp(0.4 + 0.6 * retention)
    salience = _clamp(
        0.6
        + 0.25 * float(profile["emotional_reactivity"])
        + 0.15 * float(profile["novelty_seeking"])
    )

    if acquisition_mode in {"direct_perception", "conversation"}:
        attention = _clamp(attention + 0.1)
    elif acquisition_mode in {"skim_document", "ambient"}:
        attention = _clamp(attention - 0.2)

    # Geometric-like combination keeps any single factor from dominating while
    # leaving every factor inspectable. This is a belief/recall weight, not truth.
    factors = [attention, trust, compatibility, retention_weight, salience]
    effective = _clamp(base * (prod(factors) ** (1 / len(factors))), 0.0, 1.0)

    return {
        "base_confidence": base,
        "attention_weight": attention,
        "trust_weight": trust,
        "compatibility_weight": compatibility,
        "retention_weight": retention_weight,
        "salience_weight": salience,
        "effective_confidence": effective,
        "profile_character_id": row["character_id"],
    }


async def reweight_character_knowledge(
    db: Database,
    *,
    character_id: str,
) -> int:
    rows = await db.fetch(
        """
        SELECT
            cpk.instance_id,
            cpk.proposition_id,
            cpk.acquisition_mode,
            COALESCE(cpk.base_confidence, cpk.confidence, 0.5) AS base_confidence,
            cpk.meta->>'source_key' AS source_key
        FROM aios.character_proposition_knowledge cpk
        JOIN aios.character_instance ci ON ci.instance_id=cpk.instance_id
        WHERE ci.character_id=$1
        """,
        character_id,
    )

    updated = 0
    for row in rows:
        weights = await calculate_weights(
            db,
            instance_id=row["instance_id"],
            proposition_id=row["proposition_id"],
            acquisition_mode=row["acquisition_mode"],
            base_confidence=float(row["base_confidence"]),
            source_key=row["source_key"],
        )
        await db.execute(
            """
            UPDATE aios.character_proposition_knowledge
            SET base_confidence=$3,
                attention_weight=$4,
                trust_weight=$5,
                compatibility_weight=$6,
                retention_weight=$7,
                salience_weight=$8,
                effective_confidence=$9,
                updated_at=now()
            WHERE instance_id=$1 AND proposition_id=$2
            """,
            row["instance_id"],
            row["proposition_id"],
            weights["base_confidence"],
            weights["attention_weight"],
            weights["trust_weight"],
            weights["compatibility_weight"],
            weights["retention_weight"],
            weights["salience_weight"],
            weights["effective_confidence"],
        )
        updated += 1

    return updated
