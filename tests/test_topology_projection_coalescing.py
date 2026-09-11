from aios_app.epistemic import topology, topology_claims
from aios_app.epistemic.topology_projection import deferred_project_scope_rdf
from aios_app.pipeline.job_registry import ResourceClass, job_spec, scheduling_lane, SchedulingLane


def test_per_item_topology_projection_is_deferred_for_both_paths():
    assert topology._project_scope_rdf is deferred_project_scope_rdf
    assert topology_claims._project_scope_rdf is deferred_project_scope_rdf


def test_topology_mutations_do_not_consume_rdf_slot():
    for job_type in (
        "derive_claim_topology",
        "derive_character_acquisition_topology",
        "derive_world_assertion_topology",
    ):
        spec = job_spec(job_type)
        assert spec.resource_class is ResourceClass.SEMANTIC
        assert spec.requires_rdf_slot is False


def test_scope_projection_has_one_rdf_job_type():
    spec = job_spec("project_semantic_scope")
    assert spec.resource_class is ResourceClass.RDF
    assert spec.requires_rdf_slot is True
    assert scheduling_lane("project_semantic_scope", {}) is SchedulingLane.BACKGROUND
