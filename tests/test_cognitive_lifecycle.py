from uuid import uuid4

import pytest

from aios_app.agent.cognitive_lifecycle import CognitiveLifecycleReconciler


class FakeDB:
    def __init__(self):
        self.goal_id=uuid4()
        self.instance_id=uuid4()
        self.goal_status="active"
        self.executed=[]

    async def execute(self, sql, *args):
        self.executed.append((sql,args))
        if "UPDATE aios.character_cognitive_thread" in sql:
            return "UPDATE 1"
        return "INSERT 0 1"

    async def execute_returning_row(self, sql, *args):
        self.executed.append((sql,args))
        if "UPDATE aios.character_agent_goal" in sql and self.goal_status=="active":
            self.goal_status="completed"
            return {"goal_id":self.goal_id}
        return None

    async def fetchrow(self, sql, *args):
        if "SELECT status,meta FROM aios.character_agent_goal" in sql:
            return {"status":self.goal_status,"meta":{"resolution_kind":"reviewed_satisfied"}}
        if "SELECT t.goal_id" in sql:
            return {"goal_id":self.goal_id,"goal_status":self.goal_status}
        if "SELECT goal_id FROM aios.character_cognitive_thread" in sql:
            return {"goal_id":self.goal_id}
        return None


@pytest.mark.asyncio
async def test_validated_completion_resolves_goal_linked_threads():
    db=FakeDB()
    lifecycle=CognitiveLifecycleReconciler(db)
    changed=await lifecycle.complete_goal(
        instance_id=db.instance_id,goal_id=db.goal_id,
        resolution_kind="reviewed_satisfied",evidence_type="bounded_goal_review",
        evidence_id="operation-1",confidence=.8,
    )
    assert changed is True
    assert db.goal_status=="completed"
    thread_updates=[sql for sql,_ in db.executed if "UPDATE aios.character_cognitive_thread" in sql]
    assert thread_updates
    assert "status='resolved'" in thread_updates[-1]


@pytest.mark.asyncio
async def test_operation_success_is_progress_not_automatic_completion():
    db=FakeDB()
    lifecycle=CognitiveLifecycleReconciler(db)
    thread_id=uuid4()
    operation_id=uuid4()
    await lifecycle.reconcile_operation(
        operation={"operation_id":operation_id,"instance_id":db.instance_id,
                   "thread_id":thread_id,"operation_type":"corpus.search",
                   "input":{"goal_id":str(db.goal_id)},"source_node_id":None},
        result={"kind":"inquiry","status":"complete"},
        terminal_status="succeeded",
    )
    assert db.goal_status=="active"
    evidence=[args for sql,args in db.executed if "INSERT INTO aios.character_goal_evidence" in sql]
    assert evidence
    assert evidence[0][3]=="progress"
    thread_updates=[sql for sql,_ in db.executed if "UPDATE aios.character_cognitive_thread" in sql]
    assert thread_updates
    assert "status='open'" in thread_updates[-1]


@pytest.mark.asyncio
async def test_bounded_goal_review_can_validate_completion():
    db=FakeDB()
    lifecycle=CognitiveLifecycleReconciler(db)
    await lifecycle.reconcile_operation(
        operation={"operation_id":uuid4(),"instance_id":db.instance_id,
                   "thread_id":uuid4(),"operation_type":"planning.review",
                   "input":{"goal_id":str(db.goal_id)},"source_node_id":None},
        result={"kind":"choice","option_index":2,
                "label":"The available evidence satisfies this goal."},
        terminal_status="succeeded",
    )
    assert db.goal_status=="completed"
    thread_updates=[sql for sql,_ in db.executed if "UPDATE aios.character_cognitive_thread" in sql]
    assert any("status='resolved'" in sql for sql in thread_updates)
