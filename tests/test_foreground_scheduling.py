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


def test_job_lease_prefers_unexpired_foreground_without_changing_priority():
    jobs = source("pipeline/jobs.py")
    assert "_foreground_context_for_enqueue" in jobs
    assert "q.foreground_until > now()" in jobs
    assert "dn.event_id BETWEEN pfl.first_event_id AND pfl.head_event_id" in jobs


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
    assert ") DESC, cc.created_at LIMIT $1" in frame_stage


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
