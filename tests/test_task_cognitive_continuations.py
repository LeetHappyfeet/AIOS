from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_explicit_tasks_use_task_planner():
    worker=(ROOT/"agent"/"worker.py").read_text()
    router=(ROOT/"agent"/"opportunity_router.py").read_text()
    assert "task=task" in worker
    assert "TaskOpportunityPlanner" in router

def test_task_operation_provenance_is_durable():
    tx=(ROOT/"agent"/"transactions.py").read_text()
    ops=(ROOT/"agent"/"cognitive_operations.py").read_text()
    mig=(ROOT/"migrations"/"current"/"20260928_task_cognitive_continuations.sql").read_text()
    assert 'source_task_id=tx.get("source_task_id")' in tx
    assert "source_task_id uuid" in mig
    assert "_resume_source_task" in ops

def test_selection_does_not_complete_task():
    worker=(ROOT/"agent"/"worker.py").read_text()
    assert 'transition_task(task_id, "waiting"' in worker
    assert "waiting_on_operation_id" in worker

def test_delegated_results_are_structured():
    autonomy=(ROOT/"agent"/"autonomy.py").read_text()
    assert "character_cognitive_task_dependency" in autonomy
    assert "DELEGATED COGNITION RESULT" not in autonomy
