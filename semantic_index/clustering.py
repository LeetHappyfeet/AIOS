from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from statistics import mean
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from aios_app.db import Database
from .config import SemanticIndexConfig

logger = logging.getLogger("aios.semantic_clustering")

ALGORITHM_VERSION = "semantic-cluster-v1"


@dataclass(frozen=True)
class Edge:
    a: UUID
    b: UUID
    similarity: float


@dataclass
class ClusterDraft:
    members: set[UUID]
    core_members: set[UUID]
    fringe_members: set[UUID]
    internal_edges: list[Edge]
    boundary_edges: list[Edge]


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[UUID, UUID] = {}
        self.rank: dict[UUID, int] = {}

    def add(self, value: UUID) -> None:
        if value not in self.parent:
            self.parent[value] = value
            self.rank[value] = 0

    def find(self, value: UUID) -> UUID:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, a: UUID, b: UUID) -> None:
        self.add(a)
        self.add(b)
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _cluster_key(embedding_version: str, members: set[UUID]) -> UUID:
    stable = ",".join(sorted(str(value) for value in members))
    return uuid5(
        NAMESPACE_URL,
        f"urn:aios:semantic-cluster:{ALGORITHM_VERSION}:{embedding_version}:{stable}",
    )


def _bridge_edge_indexes(edges: list[Edge]) -> set[int]:
    """Return graph-theoretic bridges in an undirected edge list."""
    adjacency: dict[UUID, list[tuple[UUID, int]]] = {}
    for idx, edge in enumerate(edges):
        adjacency.setdefault(edge.a, []).append((edge.b, idx))
        adjacency.setdefault(edge.b, []).append((edge.a, idx))

    if adjacency:
        sys.setrecursionlimit(
            max(sys.getrecursionlimit(), len(adjacency) * 2 + 100)
        )

    discovery: dict[UUID, int] = {}
    low: dict[UUID, int] = {}
    bridges: set[int] = set()
    clock = 0

    def visit(node: UUID, parent_edge: int | None) -> None:
        nonlocal clock
        clock += 1
        discovery[node] = clock
        low[node] = clock

        for neighbor, edge_idx in adjacency.get(node, []):
            if edge_idx == parent_edge:
                continue
            if neighbor not in discovery:
                visit(neighbor, edge_idx)
                low[node] = min(low[node], low[neighbor])
                if low[neighbor] > discovery[node]:
                    bridges.add(edge_idx)
            else:
                low[node] = min(low[node], discovery[neighbor])

    for node in adjacency:
        if node not in discovery:
            visit(node, None)

    return bridges


def _build_core_components(
    edges: list[Edge],
    *,
    core_threshold: float,
    min_cluster_size: int,
) -> list[set[UUID]]:
    strong_edges = [
        edge for edge in edges
        if edge.similarity >= core_threshold
    ]
    bridge_indexes = _bridge_edge_indexes(strong_edges)

    uf = UnionFind()
    for idx, edge in enumerate(strong_edges):
        # A single edge is insufficient evidence to fuse two semantic regions.
        # Removing bridges makes cores cycle/density supported instead of merely
        # connected.
        if idx in bridge_indexes:
            continue
        uf.union(edge.a, edge.b)

    groups: dict[UUID, set[UUID]] = {}
    for value in uf.parent:
        root = uf.find(value)
        groups.setdefault(root, set()).add(value)

    return [
        members
        for members in groups.values()
        if len(members) >= min_cluster_size
    ]


def _attach_fringe(
    core_components: list[set[UUID]],
    edges: list[Edge],
    *,
    attach_threshold: float,
    min_attach_links: int,
) -> tuple[list[ClusterDraft], set[UUID]]:
    adjacency: dict[UUID, list[tuple[UUID, float]]] = {}
    all_nodes: set[UUID] = set()
    for edge in edges:
        adjacency.setdefault(edge.a, []).append((edge.b, edge.similarity))
        adjacency.setdefault(edge.b, []).append((edge.a, edge.similarity))
        all_nodes.add(edge.a)
        all_nodes.add(edge.b)

    drafts = [
        ClusterDraft(
            members=set(component),
            core_members=set(component),
            fringe_members=set(),
            internal_edges=[],
            boundary_edges=[],
        )
        for component in core_components
    ]

    claimed = set().union(*(draft.members for draft in drafts)) if drafts else set()
    fringe_candidates = all_nodes - claimed

    for node in fringe_candidates:
        best_idx: int | None = None
        best_score = 0.0
        best_link_count = 0
        for idx, draft in enumerate(drafts):
            links = [
                score
                for neighbor, score in adjacency.get(node, [])
                if neighbor in draft.core_members and score >= attach_threshold
            ]
            if len(links) < min_attach_links:
                continue
            score = mean(sorted(links, reverse=True)[: min(4, len(links))])
            if score > best_score:
                best_idx = idx
                best_score = score
                best_link_count = len(links)

        if best_idx is not None and best_link_count >= min_attach_links:
            drafts[best_idx].members.add(node)
            drafts[best_idx].fringe_members.add(node)
            claimed.add(node)

    for edge in edges:
        containing = [
            idx
            for idx, draft in enumerate(drafts)
            if edge.a in draft.members or edge.b in draft.members
        ]
        for idx in containing:
            draft = drafts[idx]
            a_in = edge.a in draft.members
            b_in = edge.b in draft.members
            if a_in and b_in:
                draft.internal_edges.append(edge)
            elif a_in or b_in:
                draft.boundary_edges.append(edge)

    return drafts, all_nodes - claimed


def _cluster_metrics(
    draft: ClusterDraft,
    *,
    neighbor_k: int,
) -> tuple[float, float, float, float]:
    n = len(draft.members)
    possible_edges = n * (n - 1) / 2
    # The proposition graph is a capped k-nearest-neighbor graph, not a full
    # similarity matrix. Normalize against the maximum edge capacity that the
    # index can reasonably expose for this cluster size.
    observed_capacity = min(
        possible_edges,
        n * max(1, min(neighbor_k, n - 1)) / 2,
    )
    density = (
        min(1.0, len(draft.internal_edges) / observed_capacity)
        if observed_capacity > 0
        else 0.0
    )
    cohesion = (
        mean(edge.similarity for edge in draft.internal_edges)
        if draft.internal_edges
        else 0.0
    )
    boundary_strength = (
        max((edge.similarity for edge in draft.boundary_edges), default=0.0)
    )
    separation = max(0.0, cohesion - boundary_strength)
    return density, cohesion, boundary_strength, separation


def _membership_metrics(
    proposition_id: UUID,
    draft: ClusterDraft,
) -> tuple[float, int, UUID | None, float | None]:
    neighbors: list[tuple[UUID, float]] = []
    for edge in draft.internal_edges:
        if edge.a == proposition_id:
            neighbors.append((edge.b, edge.similarity))
        elif edge.b == proposition_id:
            neighbors.append((edge.a, edge.similarity))

    if not neighbors:
        return 0.0, 0, None, None

    neighbors.sort(key=lambda item: item[1], reverse=True)
    strongest_neighbor, strongest_similarity = neighbors[0]
    affinity = mean(score for _, score in neighbors[: min(4, len(neighbors))])
    return affinity, len(neighbors), strongest_neighbor, strongest_similarity


async def _cluster_metadata(
    db: Database,
    members: set[UUID],
) -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT
            p.proposition_id,
            p.topic_key,
            p.subject_norm,
            p.predicate_norm,
            p.created_at,
            ctx.claim_kind,
            ctx.predicate_family,
            ctx.world_id,
            ctx.timeline_id,
            obs.source_domain,
            obs.source_kind,
            obs.observed_at
        FROM aios.proposition p
        LEFT JOIN LATERAL (
            SELECT ccr.claim_kind, ccr.predicate_family, ccr.world_id, ccr.timeline_id
            FROM aios.observation o
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
            WHERE o.proposition_id=p.proposition_id
            ORDER BY ccr.resolved_at DESC
            LIMIT 1
        ) ctx ON true
        LEFT JOIN LATERAL (
            SELECT o.source_domain, o.source_kind, o.observed_at
            FROM aios.observation o
            WHERE o.proposition_id=p.proposition_id
            ORDER BY o.observed_at DESC
            LIMIT 1
        ) obs ON true
        WHERE p.proposition_id = ANY($1::uuid[])
        """,
        list(members),
    )

    def counts(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in rows:
            value = row[key]
            if value is None:
                continue
            value = str(value)
            out[value] = out.get(value, 0) + 1
        return dict(
            sorted(out.items(), key=lambda item: (-item[1], item[0]))[:12]
        )

    timestamps = [
        row["observed_at"] or row["created_at"]
        for row in rows
        if row["observed_at"] is not None or row["created_at"] is not None
    ]

    return {
        "dominant_topics": counts("topic_key"),
        "dominant_subjects": counts("subject_norm"),
        "dominant_predicates": counts("predicate_norm"),
        "claim_kinds": counts("claim_kind"),
        "predicate_families": counts("predicate_family"),
        "world_distribution": counts("world_id"),
        "timeline_distribution": counts("timeline_id"),
        "source_domains": counts("source_domain"),
        "source_kinds": counts("source_kind"),
        "time_start": min(timestamps).isoformat() if timestamps else None,
        "time_end": max(timestamps).isoformat() if timestamps else None,
    }


def _config_signature(cfg: SemanticIndexConfig) -> str:
    return "|".join([
        ALGORITHM_VERSION,
        cfg.embedding_version,
        f"core={cfg.cluster_core_threshold:.6f}",
        f"attach={cfg.cluster_attach_threshold:.6f}",
        f"floor={cfg.cluster_boundary_floor:.6f}",
        f"min_size={cfg.cluster_min_size}",
        f"min_links={cfg.cluster_min_attach_links}",
        f"min_density={cfg.cluster_min_density:.6f}",
        f"min_cohesion={cfg.cluster_min_cohesion:.6f}",
    ])


async def cluster_neighbors_once(db: Database, cfg: SemanticIndexConfig) -> int:
    watermark = await db.fetchrow(
        """
        SELECT MAX(analyzed_at) AS watermark
        FROM aios.semantic_structure_state
        WHERE embedding_version=$1
        """,
        cfg.embedding_version,
    )
    structure_watermark = watermark["watermark"] if watermark else None
    if structure_watermark is None:
        return 0

    config_signature = _config_signature(cfg)
    previous = await db.fetchrow(
        """
        SELECT structure_watermark
        FROM aios.semantic_cluster_run
        WHERE embedding_version=$1
          AND algorithm_version=$2
          AND config_signature=$3
          AND status='done'
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        cfg.embedding_version,
        ALGORITHM_VERSION,
        config_signature,
    )
    if previous and previous["structure_watermark"] is not None:
        if previous["structure_watermark"] >= structure_watermark:
            return 0

    rows = await db.fetch(
        """
        SELECT proposition_id, neighbor_proposition_id, similarity
        FROM aios.semantic_neighbor_candidate
        WHERE embedding_version=$1
          AND status='candidate'
          AND similarity >= $2
        ORDER BY similarity DESC
        """,
        cfg.embedding_version,
        cfg.cluster_boundary_floor,
    )
    edges = [
        Edge(
            a=row["proposition_id"],
            b=row["neighbor_proposition_id"],
            similarity=float(row["similarity"]),
        )
        for row in rows
    ]
    indexed_rows = await db.fetch(
        """
        SELECT p.proposition_id
        FROM aios.proposition p
        JOIN aios.semantic_vector_index_state s
          ON s.object_type='proposition'
         AND s.object_key=p.proposition_id::text
         AND s.qdrant_collection=$1
         AND s.embedding_model=$2
         AND s.embedding_version=$3
        """,
        cfg.proposition_collection,
        cfg.embedding_model,
        cfg.embedding_version,
    )
    indexed_nodes = {row["proposition_id"] for row in indexed_rows}

    core_components = _build_core_components(
        edges,
        core_threshold=cfg.cluster_core_threshold,
        min_cluster_size=cfg.cluster_min_size,
    )
    drafts, unclaimed = _attach_fringe(
        core_components,
        edges,
        attach_threshold=cfg.cluster_attach_threshold,
        min_attach_links=cfg.cluster_min_attach_links,
    )
    claimed_by_drafts = (
        set().union(*(draft.members for draft in drafts))
        if drafts else set()
    )
    unclaimed.update(indexed_nodes - claimed_by_drafts)

    run_id = uuid4()
    await db.execute(
        """
        INSERT INTO aios.semantic_cluster_run (
            run_id, embedding_version, algorithm_version,
            core_threshold, attach_threshold, min_cluster_size,
            structure_watermark, config_signature, status
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'running')
        """,
        run_id,
        cfg.embedding_version,
        ALGORITHM_VERSION,
        cfg.cluster_core_threshold,
        cfg.cluster_attach_threshold,
        cfg.cluster_min_size,
        structure_watermark,
        config_signature,
    )

    try:
        accepted_clusters = 0
        cluster_membership_map: dict[UUID, UUID] = {}
        for draft in drafts:
            density, cohesion, boundary_strength, separation = _cluster_metrics(
                draft,
                neighbor_k=cfg.neighbor_k,
            )
            if cohesion < cfg.cluster_min_cohesion:
                unclaimed.update(draft.members)
                continue
            if density < cfg.cluster_min_density:
                unclaimed.update(draft.members)
                continue

            cluster_key = _cluster_key(cfg.embedding_version, draft.members)
            cluster_id = uuid4()
            meta = await _cluster_metadata(db, draft.members)
            meta.update({
                "core_member_count": len(draft.core_members),
                "fringe_member_count": len(draft.fringe_members),
                "boundary_edge_count": len(draft.boundary_edges),
                "algorithm": ALGORITHM_VERSION,
            })

            await db.execute(
                """
                INSERT INTO aios.semantic_cluster_candidate (
                    cluster_id, cluster_key, run_id,
                    embedding_version, algorithm_version,
                    member_count, internal_edge_count, density, cohesion,
                    boundary_strength, separation, status, meta,
                    created_at, updated_at
                )
                VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                    'candidate',$12::jsonb,now(),now()
                )
                """,
                cluster_id,
                cluster_key,
                run_id,
                cfg.embedding_version,
                ALGORITHM_VERSION,
                len(draft.members),
                len(draft.internal_edges),
                density,
                cohesion,
                boundary_strength,
                separation,
                json.dumps(meta),
            )

            await db.execute(
                "DELETE FROM aios.semantic_cluster_membership WHERE cluster_id=$1",
                cluster_id,
            )
            for proposition_id in sorted(draft.members, key=str):
                affinity, degree, strongest_neighbor, strongest_similarity = (
                    _membership_metrics(proposition_id, draft)
                )
                membership_kind = (
                    "core"
                    if proposition_id in draft.core_members
                    else "fringe"
                )
                await db.execute(
                    """
                    INSERT INTO aios.semantic_cluster_membership (
                        cluster_id, proposition_id, membership_kind,
                        affinity, internal_degree,
                        strongest_neighbor_id, strongest_similarity
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    """,
                    cluster_id,
                    proposition_id,
                    membership_kind,
                    affinity,
                    degree,
                    strongest_neighbor,
                    strongest_similarity,
                )
            for proposition_id in draft.members:
                cluster_membership_map[proposition_id] = cluster_id
            accepted_clusters += 1

        boundary_groups: dict[tuple[UUID, UUID], list[float]] = {}
        for edge in edges:
            cluster_a = cluster_membership_map.get(edge.a)
            cluster_b = cluster_membership_map.get(edge.b)
            if cluster_a is None or cluster_b is None or cluster_a == cluster_b:
                continue
            pair = tuple(sorted((cluster_a, cluster_b), key=str))
            boundary_groups.setdefault(pair, []).append(edge.similarity)

        for (cluster_a, cluster_b), similarities in boundary_groups.items():
            await db.execute(
                """
                INSERT INTO aios.semantic_cluster_boundary (
                    run_id, cluster_a_id, cluster_b_id,
                    edge_count, mean_similarity, max_similarity,
                    min_similarity, meta
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,'{}'::jsonb)
                """,
                run_id,
                cluster_a,
                cluster_b,
                len(similarities),
                mean(similarities),
                max(similarities),
                min(similarities),
            )

        nearest_by_node: dict[UUID, tuple[UUID, float]] = {}
        for edge in edges:
            for node, other in ((edge.a, edge.b), (edge.b, edge.a)):
                existing = nearest_by_node.get(node)
                if existing is None or edge.similarity > existing[1]:
                    nearest_by_node[node] = (other, edge.similarity)

        outlier_count = 0
        for proposition_id in sorted(unclaimed, key=str):
            nearest = nearest_by_node.get(proposition_id)
            nearest_id = nearest[0] if nearest else None
            nearest_similarity = nearest[1] if nearest else None
            reason = (
                "weak_neighborhood"
                if nearest_similarity is not None
                and nearest_similarity < cfg.cluster_attach_threshold
                else "insufficient_cluster_support"
            )
            await db.execute(
                """
                INSERT INTO aios.semantic_outlier_candidate (
                    run_id, proposition_id, embedding_version,
                    reason, nearest_similarity, nearest_proposition_id,
                    status, meta
                )
                VALUES ($1,$2,$3,$4,$5,$6,'candidate','{}'::jsonb)
                ON CONFLICT (run_id, proposition_id) DO NOTHING
                """,
                run_id,
                proposition_id,
                cfg.embedding_version,
                reason,
                nearest_similarity,
                nearest_id,
            )
            outlier_count += 1

        await db.execute(
            """
            UPDATE aios.semantic_cluster_candidate
            SET status='stale', updated_at=now()
            WHERE embedding_version=$1
              AND algorithm_version=$2
              AND run_id <> $3
              AND status='candidate'
            """,
            cfg.embedding_version,
            ALGORITHM_VERSION,
            run_id,
        )
        await db.execute(
            """
            UPDATE aios.semantic_outlier_candidate
            SET status='stale'
            WHERE embedding_version=$1
              AND run_id <> $2
              AND status='candidate'
            """,
            cfg.embedding_version,
            run_id,
        )
        await db.execute(
            """
            UPDATE aios.semantic_cluster_run
            SET cluster_count=$2,
                outlier_count=$3,
                completed_at=now(),
                status='done'
            WHERE run_id=$1
            """,
            run_id,
            accepted_clusters,
            outlier_count,
        )
    except Exception:
        await db.execute(
            """
            UPDATE aios.semantic_cluster_run
            SET completed_at=now(), status='failed'
            WHERE run_id=$1
            """,
            run_id,
        )
        raise

    logger.info(
        "Semantic clustering produced %d clusters and %d outliers "
        "[core>=%.3f attach>=%.3f]",
        accepted_clusters,
        outlier_count,
        cfg.cluster_core_threshold,
        cfg.cluster_attach_threshold,
    )
    return accepted_clusters + outlier_count
