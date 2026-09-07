from uuid import UUID

from aios_app.semantic_index.reconciliation import (
    BOUNDARY_EDGE_TYPES,
    PAIR_EDGE_TYPES,
    _pair_source_id,
    _scope_partition,
)


def test_character_scope_partition_includes_instance():
    instance_id = UUID("11111111-1111-1111-1111-111111111111")
    row = {
        "scope_kind": "character",
        "scope_key": "char:natalie",
        "character_instance_id": instance_id,
    }
    assert _scope_partition(row) == f"char:natalie:instance:{instance_id}"


def test_non_character_scope_partition_is_scope_key():
    row = {
        "scope_kind": "world",
        "scope_key": "world:abc:asserted",
        "character_instance_id": None,
    }
    assert _scope_partition(row) == "world:abc:asserted"


def test_pair_source_id_is_order_independent():
    a = UUID("00000000-0000-0000-0000-000000000001")
    b = UUID("00000000-0000-0000-0000-000000000002")
    assert _pair_source_id(a, b, "EQUIVALENT") == _pair_source_id(
        b, a, "EQUIVALENT"
    )


def test_only_structural_neighbor_relations_have_promotion_edges():
    assert PAIR_EDGE_TYPES == {
        "EQUIVALENT": "semantic_equivalent",
        "REFINES": "semantic_refinement",
        "CONTRADICTS": "semantic_contradicts",
        "SAME_TOPIC": "semantic_same_topic",
        "SAME_EVENT": "semantic_same_event",
    }


def test_branch_classifications_remain_possible_edges():
    assert BOUNDARY_EDGE_TYPES["EXPERIENTIAL_BRANCH_CANDIDATE"] == (
        "possible_experiential_branch"
    )
    assert BOUNDARY_EDGE_TYPES["WORLD_BRANCH_CANDIDATE"] == "possible_world_branch"
