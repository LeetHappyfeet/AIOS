from aios_app.runner import _semantic_stage_reservation


def test_context_worker_reserves_resolver_stage():
    assert _semantic_stage_reservation(0) == ["resolve_claim_context"]


def test_materialization_worker_reserves_normalization_capacity():
    assert _semantic_stage_reservation(1) == [
        "normalize_proposition",
        "project_character_knowledge",
    ]


def test_structural_and_background_workers_are_not_stage_pinned():
    assert _semantic_stage_reservation(2) is None
    assert _semantic_stage_reservation(3) is None
