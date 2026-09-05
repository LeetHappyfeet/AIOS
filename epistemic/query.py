from __future__ import annotations

from uuid import UUID

from aios_app.db import Database


async def proposition_context(db: Database, *, proposition_id: UUID) -> dict:
    proposition = await db.fetchrow(
        """
        SELECT proposition_id, topic_key, canonical_text, subject_norm,
               predicate_norm, object_norm, polarity, modality, created_at, meta
        FROM aios.proposition
        WHERE proposition_id=$1
        """,
        proposition_id,
    )
    if not proposition:
        raise ValueError(f"unknown proposition {proposition_id}")

    topic_key = proposition["topic_key"]

    propositions = await db.fetch(
        """
        SELECT
            p.proposition_id, p.canonical_text, p.subject_norm,
            p.predicate_norm, p.object_norm, p.polarity, p.modality,
            count(DISTINCT o.observation_id) AS observation_count,
            count(DISTINCT o.source_key) AS source_count,
            avg(NULLIF(o.extraction_confidence,0)) AS avg_extraction_confidence
        FROM aios.proposition p
        LEFT JOIN aios.observation o ON o.proposition_id=p.proposition_id
        WHERE p.topic_key=$1
        GROUP BY p.proposition_id
        ORDER BY observation_count DESC, p.created_at
        """,
        topic_key,
    )

    sources = await db.fetch(
        """
        SELECT
            o.proposition_id, o.source_key, o.source_domain, o.source_kind,
            count(*) AS observation_count,
            max(o.observed_at) AS latest_observation
        FROM aios.observation o
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        WHERE p.topic_key=$1
        GROUP BY o.proposition_id, o.source_key, o.source_domain, o.source_kind
        ORDER BY observation_count DESC, o.source_key
        """,
        topic_key,
    )

    narratives = await db.fetch(
        """
        SELECT narrative_id, narrative_key, label, summary, confidence, meta
        FROM aios.narrative_cluster
        WHERE topic_key=$1
        ORDER BY updated_at DESC
        """,
        topic_key,
    )
    narrative_rows = []
    for narrative in narratives:
        affinities = await db.fetch(
            """
            SELECT source_key, observation_count, affinity
            FROM aios.narrative_source_affinity
            WHERE narrative_id=$1
            ORDER BY affinity DESC, observation_count DESC, source_key
            """,
            narrative["narrative_id"],
        )
        item = dict(narrative)
        item["source_affinities"] = [dict(a) for a in affinities]
        narrative_rows.append(item)

    conflicts = await db.fetch(
        """
        SELECT conflict_id, proposition_a_id, proposition_b_id,
               conflict_type, strength, detected_at, meta
        FROM aios.proposition_conflict
        WHERE topic_key=$1
        ORDER BY strength DESC, detected_at
        """,
        topic_key,
    )

    return {
        "focus": dict(proposition),
        "topic_key": topic_key,
        "propositions": [dict(p) for p in propositions],
        "sources": [dict(s) for s in sources],
        "narratives": narrative_rows,
        "conflicts": [dict(row) for row in conflicts],
    }


async def world_epistemic_state(db: Database, *, world_id: UUID) -> dict:
    world = await db.fetchrow(
        "SELECT world_id, world_key, world_type FROM aios.world WHERE world_id=$1",
        world_id,
    )
    if not world:
        raise ValueError(f"unknown world {world_id}")

    assertions = await db.fetch(
        """
        SELECT
            a.assertion_id, a.epistemic_status, a.source_kind, a.confidence,
            a.reason, a.superseded_by_assertion_id, a.created_at, a.updated_at,
            p.proposition_id, p.topic_key, p.canonical_text,
            p.subject_norm, p.predicate_norm, p.object_norm, p.polarity
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        WHERE a.world_id=$1
        ORDER BY a.updated_at DESC
        """,
        world_id,
    )
    return {
        "world": dict(world),
        "assertions": [dict(a) for a in assertions],
    }
