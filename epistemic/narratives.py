from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from uuid import UUID

from aios_app.db import Database

NARRATIVE_VERSION = "narrative-v1"


def _narrative_key(*, polarity: int, object_norm: str | None, proposition_id: UUID) -> str:
    # First generation: group observations that make the same normalized
    # proposition/value claim. This reveals source alignment without creating
    # alternate worlds merely because sources frame a topic differently.
    material = f"{polarity}\x1f{object_norm or ''}\x1f{proposition_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def assign_narratives_once(db: Database, *, limit: int = 500) -> int:
    rows = await db.fetch(
        """
        SELECT
            o.observation_id, o.proposition_id, o.source_key,
            p.topic_key, p.canonical_text, p.object_norm, p.polarity
        FROM aios.observation o
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.narrative_membership nm
            WHERE nm.observation_id=o.observation_id
        )
        ORDER BY o.observed_at
        LIMIT $1
        """,
        limit,
    )
    if not rows:
        return 0

    touched: set[UUID] = set()
    assigned = 0

    for row in rows:
        nkey = _narrative_key(
            polarity=int(row["polarity"]),
            object_norm=row["object_norm"],
            proposition_id=row["proposition_id"],
        )
        cluster = await db.execute_returning_row(
            """
            INSERT INTO aios.narrative_cluster (
                topic_key, narrative_key, label, summary, confidence, meta
            )
            VALUES ($1,$2,$3,$4,0.5,$5::jsonb)
            ON CONFLICT (topic_key, narrative_key) DO UPDATE
            SET updated_at=now()
            RETURNING narrative_id
            """,
            row["topic_key"],
            nkey,
            row["canonical_text"][:160],
            row["canonical_text"],
            json.dumps({"clusterer": NARRATIVE_VERSION}),
        )
        narrative_id = cluster["narrative_id"]
        touched.add(narrative_id)

        await db.execute(
            """
            INSERT INTO aios.narrative_membership (
                narrative_id, observation_id, affinity, assigned_by
            )
            VALUES ($1,$2,1.0,$3)
            ON CONFLICT (narrative_id, observation_id) DO NOTHING
            """,
            narrative_id,
            row["observation_id"],
            NARRATIVE_VERSION,
        )
        assigned += 1

    # Recompute source affinities for affected narratives. Affinity is the
    # fraction of that source's observations on the topic falling in the
    # narrative; raw counts stay visible so averaging never hides disagreement.
    for narrative_id in touched:
        topic = await db.fetchrow(
            "SELECT topic_key FROM aios.narrative_cluster WHERE narrative_id=$1",
            narrative_id,
        )
        stats = await db.fetch(
            """
            WITH source_topic AS (
                SELECT o.source_key, count(*)::double precision AS total
                FROM aios.observation o
                JOIN aios.proposition p ON p.proposition_id=o.proposition_id
                WHERE p.topic_key=$1
                GROUP BY o.source_key
            ),
            source_narrative AS (
                SELECT o.source_key, count(*)::integer AS cnt
                FROM aios.narrative_membership nm
                JOIN aios.observation o ON o.observation_id=nm.observation_id
                WHERE nm.narrative_id=$2
                GROUP BY o.source_key
            )
            SELECT sn.source_key, sn.cnt, st.total,
                   sn.cnt::double precision / NULLIF(st.total,0) AS affinity
            FROM source_narrative sn
            JOIN source_topic st USING (source_key)
            """,
            topic["topic_key"],
            narrative_id,
        )
        for stat in stats:
            await db.execute(
                """
                INSERT INTO aios.narrative_source_affinity (
                    narrative_id, source_key, observation_count, affinity
                )
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (narrative_id, source_key) DO UPDATE
                SET observation_count=EXCLUDED.observation_count,
                    affinity=EXCLUDED.affinity,
                    updated_at=now()
                """,
                narrative_id,
                stat["source_key"],
                stat["cnt"],
                float(stat["affinity"] or 0.0),
            )

    return assigned
