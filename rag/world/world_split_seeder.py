from __future__ import annotations

import json
import logging
from uuid import UUID, uuid5, NAMESPACE_URL

from aios_app.db import Database

logger = logging.getLogger("aios.world.split_seeder")


async def seed_world_splits_from_similarity(
    db: Database,
    *,
    similarity_threshold: float = 0.70,
    limit: int = 500,
) -> int:
    """
    Backward-compatible entrypoint with stricter semantics.

    Similarity/narrative divergence no longer creates possible worlds. A world
    split candidate is seeded only from an explicit normalized proposition
    conflict (opposite polarity or a competing exclusive value).

    similarity_threshold is retained for API compatibility but is not used.
    """
    conflicts = await db.fetch(
        """
        SELECT
            pc.conflict_id, pc.proposition_a_id, pc.proposition_b_id,
            pc.conflict_type, pc.strength, pc.detected_at,
            oa.claim_id AS claim_a_id, ob.claim_id AS claim_b_id,
            dsa.section_id AS section_a_id, dsb.section_id AS section_b_id,
            oa.observed_at AS observed_a_at, ob.observed_at AS observed_b_at
        FROM aios.proposition_conflict pc
        JOIN LATERAL (
            SELECT o.claim_id, o.dag_node_id, o.observed_at
            FROM aios.observation o
            WHERE o.proposition_id=pc.proposition_a_id
            ORDER BY o.observed_at
            LIMIT 1
        ) oa ON true
        JOIN LATERAL (
            SELECT o.claim_id, o.dag_node_id, o.observed_at
            FROM aios.observation o
            WHERE o.proposition_id=pc.proposition_b_id
            ORDER BY o.observed_at
            LIMIT 1
        ) ob ON true
        JOIN aios.document_section dsa ON dsa.node_id=oa.dag_node_id
        JOIN aios.document_section dsb ON dsb.node_id=ob.dag_node_id
        ORDER BY pc.detected_at
        LIMIT $1
        """,
        limit,
    )

    inserted = 0
    for row in conflicts:
        split_id = uuid5(
            NAMESPACE_URL,
            f"urn:aios:proposition-conflict:{row['conflict_id']}",
        )
        boundary = [{
            "conflict_id": str(row["conflict_id"]),
            "proposition_a_id": str(row["proposition_a_id"]),
            "proposition_b_id": str(row["proposition_b_id"]),
            "conflict_type": row["conflict_type"],
            "strength": float(row["strength"]),
        }]
        cluster_a = [str(row["claim_a_id"])]
        cluster_b = [str(row["claim_b_id"])]

        result = await db.execute(
            """
            INSERT INTO aios.world_split_candidate (
                split_id, seed_section_id, window_start, window_end,
                cluster_count, cluster_a, cluster_b,
                centroid_distance, boundary_pairs
            )
            VALUES (
                $1,$2,
                LEAST($3::timestamptz,$4::timestamptz),
                GREATEST($3::timestamptz,$4::timestamptz),
                2,$5::jsonb,$6::jsonb,$7,$8::jsonb
            )
            ON CONFLICT (split_id) DO NOTHING
            """,
            split_id,
            row["section_a_id"],
            row["observed_a_at"],
            row["observed_b_at"],
            json.dumps(cluster_a),
            json.dumps(cluster_b),
            float(row["strength"]),
            json.dumps(boundary),
        )
        if result.endswith("1"):
            inserted += 1

        await db.execute(
            """
            INSERT INTO aios.section_cluster_assignment (
                split_id, section_id, cluster_label, score_to_centroid
            )
            VALUES ($1,$2,'A',$3)
            ON CONFLICT (split_id, section_id) DO NOTHING
            """,
            split_id,
            row["section_a_id"],
            float(row["strength"]),
        )
        await db.execute(
            """
            INSERT INTO aios.section_cluster_assignment (
                split_id, section_id, cluster_label, score_to_centroid
            )
            VALUES ($1,$2,'B',$3)
            ON CONFLICT (split_id, section_id) DO NOTHING
            """,
            split_id,
            row["section_b_id"],
            float(row["strength"]),
        )

    logger.info("Seeded %d world splits from explicit proposition conflicts", inserted)
    return inserted
