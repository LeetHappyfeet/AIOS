from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_foreground_schema_is_a_lease_not_a_lane():
    sql = source("migrations/current/20260928_foreground_causal_scheduling.sql")
    assert "pipeline_foreground_lineage" in sql
    assert "expires_at" in sql
    assert "foreground_until" in sql
    assert "scheduling_lane" not in sql.split("CREATE TABLE IF NOT EXISTS aios.pipeline_foreground_lineage", 1)[1].split(");", 1)[0]


def test_ingest_refreshes_foreground_from_exact_affected_instances():
    ingest = source("ingest_api.py")
    assert "affected_instance_ids = await mark_matching_runtime_dirty" in ingest
    assert "INSERT INTO aios.pipeline_foreground_lineage" in ingest
    assert "LEAST(aios.pipeline_foreground_lineage.first_event_id" in ingest


def test_job_claim_uses_current_source_lineage_for_queued_descendants():
    jobs = source("pipeline/jobs.py")
    assert "_foreground_context_for_enqueue" in jobs
    assert "LEFT JOIN LATERAL" in jobs
    assert "FOR UPDATE OF q SKIP LOCKED" in jobs
    assert "current_foreground.updated_at DESC NULLS LAST" in jobs
    assert "kae.instance_id" in jobs
    assert "dn.event_id BETWEEN pfl.first_event_id AND pfl.head_event_id" in jobs


def test_acquisition_admission_prioritizes_latest_source_and_reserves_backlog():
    supervisor = source("supervisor.py")
    stage = supervisor.split('Stage("derive_character_acquisition_topology"', 1)[1].split(
        'Stage("derive_world_assertion_topology"', 1
    )[0]
    assert "foreground_at DESC" in stage
    assert "ceil($1::numeric*0.75)" in stage
    assert "ORDER BY e.created_at,e.acquisition_id" in stage
    assert "pfl.instance_id=kae.instance_id" in stage


def test_foreground_renewal_keeps_activity_order_and_backfills_live_source():
    supervisor = source("supervisor.py")
    refresh = supervisor.split("async def renew_unfinished_foreground", 1)[1].split(
        "def stage_admission_capacity", 1
    )[0]
    assert "SET expires_at=" in refresh
    assert "SET expires_at=now()+interval '1 hour'" in refresh
    assert "updated_at=" not in refresh.split('await db.execute("""', 1)[1]
    migration = source("migrations/current/20260929_foreground_source_continuation.sql")
    assert "WHERE hr.live" in migration
    assert "ON CONFLICT (instance_id) DO NOTHING" in migration


def test_character_convergence_is_instance_scoped():
    supervisor = source("supervisor.py")
    runner = source("runner.py")
    beliefs = source("epistemic/belief_reconciliation.py")
    assert 'def live_instance_payload' in supervisor
    assert 'pj.payload->>\'live_instance_id\'=kae.instance_id::text' in supervisor
    assert 'pj.payload->>\'live_instance_id\'=d.instance_id::text' in supervisor
    assert 'live_instance_id = (job.get("payload") or {}).get("live_instance_id")' in runner
    assert "instance_id: UUID | None = None" in beliefs
    assert "WHERE ($2::uuid IS NULL OR instance_id=$2)" in beliefs


def test_foreground_discovery_precedes_old_nlp_backlog():
    supervisor = source("supervisor.py")
    frame_stage = supervisor.split('Stage("decompose_claim_frames"', 1)[1].split('Stage("resolve_claim_context"', 1)[0]
    assert "pipeline_foreground_lineage" in frame_stage
    assert "foreground AS (" in frame_stage
    assert "0 AS band_order" in frame_stage
    assert "ORDER BY band_order,created_at,claim_id" in frame_stage


def test_decomposition_discovery_reserves_old_and_fresh_capacity():
    supervisor = source("supervisor.py")
    frame_stage = supervisor.split('Stage("decompose_claim_frames"', 1)[1].split(
        'Stage("resolve_claim_context"', 1
    )[0]
    assert "backlog_quota" in frame_stage
    assert "fresh_quota" in frame_stage
    assert "*0.75" in frame_stage
    assert "ORDER BY e.created_at,e.claim_id" in frame_stage
    assert "ORDER BY e.created_at DESC,e.claim_id" in frame_stage
    assert "NOT EXISTS (SELECT 1 FROM backlog b WHERE b.claim_id=e.claim_id)" in frame_stage


def test_participant_binding_is_authoritative_for_source_cursor():
    cursor = source("world/source_cursor.py")
    assert "SELECT character_instance_id" in cursor
    assert "bound_instance_id = (" in cursor
    assert "WHERE rs.instance_id=$1" in cursor
    assert "participant-aware timelines" in cursor
    assert "Compatibility path for legacy single-character timelines" in cursor


def test_bound_participant_never_silently_falls_back():
    cursor = source("world/source_cursor.py")
    assert "conversation participant has no character instance binding" in cursor
    assert "but that instance has no runtime state" in cursor


def test_claim_extraction_owns_semantic_decomposition_handoff():
    worker = source("pipeline/worker.py")
    assert "async def _ensure_decomposition_fanout" in worker
    assert 'job_type="decompose_claim_frames"' in worker
    assert "claim_semantic_frame_projection" in worker
    assert "decomposer_version='semantic-frame-v2'" in worker
    assert "await _ensure_decomposition_fanout(db, section_id=section_id)" in worker
    assert "from aios_app.pipeline.jobs import enqueue_job" in worker


def test_completed_claim_sections_repair_missing_decomposition_jobs():
    worker = source("pipeline/worker.py")
    completed = worker.split('if row["claims_extracted_at"] is not None:', 1)[1].split(
        "document_id:", 1
    )[0]
    assert "_ensure_decomposition_fanout" in completed
    assert "_mark_claim_stage_complete" in completed


def test_supervisor_remains_decomposition_repair_path():
    supervisor = source("supervisor.py")
    frame_stage = supervisor.split('Stage("decompose_claim_frames"', 1)[1].split(
        'Stage("resolve_claim_context"', 1
    )[0]
    assert "claim_semantic_frame_projection" in frame_stage
    assert "pipeline_job" in frame_stage


def test_source_semantic_pipeline_uses_producer_driven_handoffs():
    section_worker = source("pipeline/dag_to_document_section_worker.py")
    claim_worker = source("pipeline/worker.py")
    runner = source("runner.py")

    assert 'job_type="extract_claims"' in section_worker
    assert 'job_type="decompose_claim_frames"' in claim_worker

    decompose = runner.split("async def handle_decompose_claim_frames", 1)[1].split(
        "async def handle_normalize_proposition", 1
    )[0]
    assert 'job_type="resolve_claim_context"' in decompose
    assert '"admission_band": "producer"' in decompose

    resolve = runner.split("async def handle_resolve_claim_context", 1)[1].split(
        "JOB_HANDLERS.update", 1
    )[0]
    assert 'job_type="normalize_proposition"' in resolve
