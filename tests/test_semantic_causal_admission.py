from uuid import uuid4

import pytest

from aios_app.causal.semantic_bridge import admit_location_claim


class PolicyDB:
    def __init__(self,scope="character",predicate="enter"):
        self.scope=scope; self.predicate=predicate
    async def fetchrow(self,sql,*args):
        if "epistemic_scope" in sql:
            return {"epistemic_scope":self.scope,"claim_kind":"EVENT",
                    "predicate_family":"SPATIAL","world_id":None,"timeline_id":None,
                    "predicate":self.predicate,"resolution_status":"resolved"}
        raise AssertionError("epistemic-only claim must stop before entity compilation")


class NeverKernel:
    async def commit(self,*args,**kwargs):
        raise AssertionError("epistemic-only claim must never reach causal commit")


@pytest.mark.asyncio
async def test_character_spatial_claim_stays_epistemic():
    result=await admit_location_claim(
        PolicyDB("character","enter"),NeverKernel(),claim_id=uuid4(),
        world_id=uuid4(),timeline_id=uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_world_endpoint_assertion_is_not_promoted_as_movement():
    result=await admit_location_claim(
        PolicyDB("world","be_in"),NeverKernel(),claim_id=uuid4(),
        world_id=uuid4(),timeline_id=uuid4())
    assert result is None
