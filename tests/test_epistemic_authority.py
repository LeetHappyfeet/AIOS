from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_authority_migration_has_immutable_origin_and_lineage():
    sql = (ROOT / "migrations/current/20260927_epistemic_authority_membrane.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS aios.epistemic_authority_admission" in sql
    assert "generated_cognition_has_no_historical_authority" in sql
    assert "Origin and lineage deliberately never change" in sql
    update = sql.split("ON CONFLICT (acquisition_id) DO UPDATE SET", 1)[1]
    assert "origin_kind=EXCLUDED.origin_kind" not in update
    assert "lineage_key=EXCLUDED.lineage_key" not in update


def test_generated_cognition_is_subjective_only_by_default():
    sql = (ROOT / "migrations/current/20260927_epistemic_authority_membrane.sql").read_text()
    assert "v_origin IN ('model_generation','model_inference','character_generation')" in sql
    assert "v_uses:=ARRAY['belief','reflection']" in sql
    assert "action_precondition" in sql


def test_record_acquisition_accepts_explicit_provenance_contract():
    source = (ROOT / "epistemic/knowledge.py").read_text()
    assert "origin_kind: Optional[str] = None" in source
    assert "epistemic_mode: Optional[str] = None" in source
    assert "origin_lineage_id: Optional[str] = None" in source
    assert 'acquisition_meta["origin_kind"] = origin_kind' in source


def test_downstream_authority_enforcement_is_wired():
    root = Path(__file__).resolve().parents[1]
    retrieval = (root / "epistemic" / "retrieval.py").read_text()
    actions = (root / "agent" / "actions.py").read_text()
    render = (root / "hud" / "render_text.py").read_text()
    migration = (root / "migrations" / "current" / "20260928_epistemic_authority_enforcement.sql").read_text()
    assert "epistemic_authority_admission" in retrieval
    assert "authorized_uses" in retrieval
    assert "epistemic_precondition_unsatisfied" in actions
    assert "required_authority_use" in actions
    assert "_epistemic_label" in render
    assert "COALESCE(eaa.lineage_key" in migration
    assert "'belief'=ANY(eaa.authorized_uses)" in migration
    assert "trg_expand_user_action_authority" in migration


def test_corpus_re_reads_share_lineage():
    root = Path(__file__).resolve().parents[1]
    corpus = (root / "corpus.py").read_text()
    assert '"evidence_correlation_key": f"corpus-section:{section_id}"' in corpus


def test_sillytavern_character_turns_are_generated_cognition():
    sql = (ROOT / "migrations/current/20260928_character_generation_authority.sql").read_text()
    assert "lower(COALESCE(r.source_kind,''))='sillytavern_chat'" in sql
    assert "lower(COALESCE(r.speaker_role::text,''))='character'" in sql
    assert "THEN 'character_generation'" in sql
    assert "v_origin IN ('model_generation','model_inference','character_generation')" in sql
    assert "generated_cognition_has_no_historical_authority" in sql


def test_applied_authority_migration_is_not_used_for_followup_policy_changes():
    baseline = (ROOT / "migrations/current/20260927_epistemic_authority_membrane.sql").read_text()
    forward = (ROOT / "migrations/current/20260928_character_generation_authority.sql").read_text()
    assert "sillytavern_chat" not in baseline
    assert "SillyTavern" in forward


def test_rdf_observations_project_authority_membrane():
    source = (ROOT / "rdf" / "epistemic_writer.py").read_text()
    assert "epistemic_authority_admission" in source
    assert "world:authorityState" not in source  # rendered through the shared prefix helper
    assert '"originKind", "epistemicMode", "authorityState", "authorityRank"' in source
    assert '"lineageKey", "predicateClass", "authorityPolicyVersion"' in source
    assert '"authorizedUse", "rdfProjectionVersion"' in source
    assert '_project_observation_authority(' in source
    assert 'prefix="world"' in source
    assert 'prefix="char"' in source
    assert 'EPISTEMIC_RDF_VERSION = "epistemic-authority-rdf-v1"' in source
    # Authority refresh must happen before the legacy receipt early-return so
    # previously projected observations can be upgraded on their next pass.
    refresh = source.index("await _project_observation_authority(")
    receipt_return = source.index("if world_receipt and (not character_id or char_receipt):")
    assert refresh < receipt_return


def test_rdf_projection_is_scheduler_coalesced():
    projection = (ROOT / "epistemic" / "topology_projection.py").read_text()
    runner = (ROOT / "runner_v2.py").read_text()

    assert "RDF_PROJECTION_QUIET_SECONDS = 30.0" in projection

    mark_start = projection.index("async def mark_scope_dirty")
    mark_end = projection.index("async def deferred_project_scope_rdf", mark_start)
    mark_body = projection[mark_start:mark_end]
    assert "enqueue_job(" not in mark_body
    assert "UPDATE aios.pipeline_job" not in mark_body
    assert "dirty_version=aios.semantic_scope_projection_state.dirty_version + 1" in mark_body

    scheduler_start = projection.index("async def enqueue_dirty_scope_jobs")
    scheduler_body = projection[scheduler_start:]
    assert "dirty_version > s.projected_version" in scheduler_body
    assert "pj.status IN ('queued','running')" in scheduler_body
    assert "RDF_PROJECTION_QUIET_SECONDS" in scheduler_body
    assert 'job_type="project_semantic_scope"' in scheduler_body

    assert "RDF_PROJECTION_SCHEDULER_SECONDS = 5.0" in runner
    assert "await asyncio.sleep(RDF_PROJECTION_SCHEDULER_SECONDS)" in runner


def test_rdf_projection_preserves_mutation_during_publish():
    projection = (ROOT / "epistemic" / "topology_projection.py").read_text()
    assert "target_version = int(state[\"dirty_version\"] or 0)" in projection
    assert "projected_version=GREATEST(projected_version,$4)" in projection
    assert "WHEN dirty_version <= $4 THEN 'ready' ELSE 'dirty'" in projection
    assert "rdf_change_cursor=GREATEST(rdf_change_cursor,$5)" in projection


def test_character_epistemic_rdf_does_not_shadow_into_world():
    writer = (ROOT / "rdf" / "epistemic_writer.py").read_text()
    assert "character_owned = bool(character_id)" in writer
    assert "if not character_owned:" in writer
    assert "if not character_owned and not world_receipt:" in writer
    assert "if character_owned and world_receipt:" in writer
    assert "FILTER NOT EXISTS" in writer
    assert "DELETE FROM aios.rdf_promotion_log" in writer


def test_world_epistemic_projection_remains_for_objective_observations():
    writer = (ROOT / "rdf" / "epistemic_writer.py").read_text()
    assert 'GRAPH <{GRAPH_IRI}>' in writer
    assert '<{obs_iri}> a world:Observation' in writer
    assert 'world:observesProposition <{prop_iri}>' in writer
    assert "if not character_owned and not world_receipt:" in writer


def test_legacy_character_world_shadows_have_bounded_background_cleanup():
    writer = (ROOT / "rdf" / "epistemic_writer.py").read_text()
    runner = (ROOT / "runner_v2.py").read_text()
    registry = (ROOT / "pipeline" / "job_registry.py").read_text()

    assert "async def compact_character_world_epistemic_shadows" in writer
    assert "ccr.origin_character_id IS NOT NULL" in writer
    assert "LIMIT $4" in writer
    assert "compact_character_world_epistemic" in runner
    assert "pj.status IN ('queued','running')" in runner
    assert '"compact_character_world_epistemic": JobSpec(ResourceClass.RDF' in registry
    assert '{"project_semantic_scope", "compact_character_world_epistemic"}' in registry
