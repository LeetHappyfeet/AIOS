from aios_app.supervisor import STAGES, Stage, stage_admission_capacity


def _stage(name: str) -> Stage:
    return next(stage for stage in STAGES if stage.name == name)


def test_critical_stage_can_use_reserved_capacity():
    stage = _stage("resolve_claim_context")
    allowed = stage_admission_capacity(
        stage=stage,
        total_queued=513,
        stage_queued=0,
        batch_size=25,
        remaining_cycle=50,
        soft_cap=500,
        critical_reserve=128,
    )
    assert allowed == 25


def test_background_stage_stops_at_soft_cap():
    stage = _stage("derive_claim_topology")
    allowed = stage_admission_capacity(
        stage=stage,
        total_queued=513,
        stage_queued=0,
        batch_size=25,
        remaining_cycle=50,
        soft_cap=500,
        critical_reserve=128,
    )
    assert allowed == 0


def test_stage_queue_limit_prevents_projection_flood():
    stage = _stage("rdf_epistemic_project")
    allowed = stage_admission_capacity(
        stage=stage,
        total_queued=100,
        stage_queued=64,
        batch_size=25,
        remaining_cycle=50,
        soft_cap=500,
        critical_reserve=128,
    )
    assert allowed == 0


def test_prerequisites_outrank_projection_enrichment():
    assert _stage("resolve_claim_context").priority < _stage("normalize_proposition").priority
    assert _stage("normalize_proposition").priority < _stage("rdf_epistemic_project").priority
    assert _stage("project_character_knowledge").priority < _stage("derive_claim_topology").priority


def test_character_acquisition_topology_has_reserved_capacity():
    stage = _stage("derive_character_acquisition_topology")
    assert stage.critical is True
    assert stage.queue_limit <= 64
