from uuid import UUID

from aios_app.semantic_index.clustering import (
    Edge,
    _attach_fringe,
    _build_core_components,
)


def u(n: int) -> UUID:
    return UUID(int=n)


def test_single_strong_bridge_does_not_merge_dense_cores():
    edges = [
        Edge(u(1), u(2), 0.91),
        Edge(u(2), u(3), 0.90),
        Edge(u(1), u(3), 0.89),
        Edge(u(4), u(5), 0.93),
        Edge(u(5), u(6), 0.92),
        Edge(u(4), u(6), 0.90),
        Edge(u(3), u(4), 0.86),
    ]

    components = _build_core_components(
        edges,
        core_threshold=0.82,
        min_cluster_size=3,
    )

    assert {frozenset(c) for c in components} == {
        frozenset({u(1), u(2), u(3)}),
        frozenset({u(4), u(5), u(6)}),
    }


def test_fringe_requires_multiple_supporting_links():
    core = [{u(1), u(2), u(3)}]
    edges = [
        Edge(u(1), u(2), 0.90),
        Edge(u(2), u(3), 0.89),
        Edge(u(1), u(3), 0.88),
        Edge(u(4), u(1), 0.80),
        Edge(u(4), u(2), 0.79),
        Edge(u(5), u(1), 0.81),
    ]

    drafts, outliers = _attach_fringe(
        core,
        edges,
        attach_threshold=0.76,
        min_attach_links=2,
    )

    assert u(4) in drafts[0].fringe_members
    assert u(4) in drafts[0].members
    assert u(5) not in drafts[0].members
    assert u(5) in outliers
