"""Stage-specific diagnosis should not infer goals from shadow populations."""
import asyncio
from uuid import uuid4
from aios_app.epistemic.cognition_diagnostics import diagnose_goal_pipeline


class FakeDB:
    def __init__(self, rows):
        self.rows=rows
        self.queries=[]
    async def fetch(self,query,*args):
        self.queries.append(query)
        if "WITH RECURSIVE chain" in query:
            return self.rows
        if "FROM aios.character_agent_goal" in query:
            return []
        raise AssertionError(query)
    async def fetchrow(self,query,*args):
        self.queries.append(query)
        if "character_hud_readiness" in query:
            return None
        if "character_participation_evaluation" in query:
            return {"evaluated":1,"evaluations_with_goals":0,
                    "first_evaluation":None,"last_evaluation":None}
        raise AssertionError(query)


def row(*, committed=True, positive=0, pending=False):
    return {"event_id":11,"node_id":uuid4(),"speaker_id":"Character_A",
        "interpreter_version":"cognition-test-v1" if committed else None,
        "summary":{"candidate_outcome":"zero_units_explained",
                   "enrichment_pending":pending,"candidate_rejections":[]}
                  if committed else None,
        "enrichment_completed_at":None,"units":positive,"goal_units":positive,
        "positive_owned_goal_units":positive,"reviewed_goal_units":0}


def test_distinguishes_missing_commit_from_committed_zero_units():
    db=FakeDB([row(committed=False),row()])
    report=asyncio.run(diagnose_goal_pipeline(db,instance_id=uuid4()))
    assert report["source_nodes_without_commit"]==1
    assert report["committed_zero_unit_nodes"]==1
    assert report["first_observed_blocker"]=="source_nodes_without_cognition_receipt"
    assert any("dag_edge" in query for query in db.queries)


def test_absent_managed_goal_with_positive_unit_is_not_called_no_intention():
    db=FakeDB([row(positive=1)])
    report=asyncio.run(diagnose_goal_pipeline(
        db,instance_id=uuid4(),experiment_id=uuid4()))
    assert report["positive_owned_goal_units"]==1
    assert report["first_observed_blocker"]=="goal_admission_or_terminal_lifecycle"
    assert report["participation_at_evaluation"]["evaluations_with_goals"]==0


def test_pending_inference_is_reported_before_absent_positive_goal():
    db=FakeDB([row(pending=True)])
    report=asyncio.run(diagnose_goal_pipeline(db,instance_id=uuid4()))
    assert report["first_observed_blocker"]=="bounded_enrichment_incomplete"
