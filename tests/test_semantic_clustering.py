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
        Edge(u(1), u(2), 0.91, "SAME_TOPIC"),
        Edge(u(2), u(3), 0.90, "SAME_TOPIC"),
        Edge(u(1), u(3), 0.89, "SAME_TOPIC"),
        Edge(u(4), u(5), 0.93, "SAME_TOPIC"),
        Edge(u(5), u(6), 0.92, "SAME_TOPIC"),
        Edge(u(4), u(6), 0.90, "SAME_TOPIC"),
        Edge(u(3), u(4), 0.86, "SAME_TOPIC"),
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


def test_related_edges_do_not_form_core_components():
    a = UUID("00000000-0000-0000-0000-000000000001")
    b = UUID("00000000-0000-0000-0000-000000000002")
    c = UUID("00000000-0000-0000-0000-000000000003")
    edges = [
        Edge(a, b, 0.95, "RELATED"),
        Edge(b, c, 0.95, "RELATED"),
        Edge(c, a, 0.95, "RELATED"),
    ]
    assert _build_core_components(
        edges,
        core_threshold=0.82,
        min_cluster_size=3,
    ) == []


def test_indirect_positive_paths_cannot_join_contradictory_core_members():
    edges = [Edge(u(a), u(b), .95 if (a,b) == (1,2) else .9, "SAME_TOPIC")
             for a,b in [(1,2),(1,3),(1,4),(2,3),(2,4),(3,4)]]
    edges = [edge for edge in edges if {edge.a,edge.b} != {u(1),u(2)}]
    edges.append(Edge(u(1),u(2),.96,"CONTRADICTS"))
    groups = _build_core_components(edges, core_threshold=.82, min_cluster_size=2)
    assert groups
    assert all(not {u(1),u(2)} <= group for group in groups)


def test_fringe_conflict_with_any_member_blocks_attachment():
    edges = [Edge(u(4),u(1),.9,"RELATED"), Edge(u(4),u(2),.9,"RELATED"),
             Edge(u(4),u(3),.95,"CONTRADICTS")]
    drafts, outliers = _attach_fringe([{u(1),u(2),u(3)}], edges,
                                     attach_threshold=.76,min_attach_links=2)
    assert u(4) in outliers
    assert u(4) not in drafts[0].members


def test_two_conflicting_fringe_members_do_not_both_attach():
    edges = [Edge(u(n),u(c),.9,"RELATED") for n in [4,5] for c in [1,2]]
    edges.append(Edge(u(4),u(5),.98,"CONTRADICTS"))
    drafts, outliers = _attach_fringe([{u(1),u(2),u(3)}], edges,
                                     attach_threshold=.76,min_attach_links=2)
    assert drafts[0].fringe_members == {u(4)}
    assert u(5) in outliers
