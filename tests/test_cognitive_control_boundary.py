from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cognitive_operation_registry_is_separate_from_actions():
    text=(ROOT/"agent"/"cognitive_operation_registry.py").read_text()
    assert '"memory.retrieve"' in text
    assert '"planning.review"' in text
    assert "ActionRegistry" not in text
    assert "default_action_registry" not in text


def test_opportunities_cross_cognitive_operation_boundary():
    text=(ROOT/"agent"/"opportunities.py").read_text()
    assert "CognitiveOperationRegistry" in text
    assert "self.cognitive_operations.prepare(" in text
    assert "PreparedOperationBoundary" not in text
    assert "default_action_registry" not in text


def test_worker_owns_synchronous_transaction_once():
    worker=(ROOT/"agent"/"worker.py").read_text()
    router=(ROOT/"agent"/"opportunity_router.py").read_text()
    assert "enqueue_inference=False" in worker
    assert "source_task_id=task.task_id" in worker
    assert "if enqueue_inference:" in router


def test_inference_cannot_receive_action_schemas_from_registry():
    actions=(ROOT/"agent"/"actions.py").read_text()
    worker=(ROOT/"agent"/"worker.py").read_text()
    assert "def schemas_for(" not in actions
    assert "allowed_actions=schemas" not in worker
    assert "response.actions" not in worker
