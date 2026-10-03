"""Regression checks: source role and cognitive identity are independent."""
from uuid import uuid4

import pytest

from aios_app.world.conversation import ensure_participant, bind_available_instance


class ParticipantDB:
    def __init__(self, identity=None):
        self.identity = identity
        self.statements = []

    async def fetchrow(self, sql, *args):
        self.statements.append((sql, args))
        if "FROM aios.character_identity WHERE character_id=$1" in sql:
            return {"character_id": self.identity} if self.identity else None
        return None

    async def execute_returning_row(self, sql, *args):
        self.statements.append((sql, args))
        if "INSERT INTO aios.conversation_participant" in sql:
            return {"participant_id": uuid4()}
        return {"character_instance_id": uuid4()}


@pytest.mark.asyncio
async def test_user_with_explicit_cognitive_identity_can_bind():
    db = ParticipantDB(identity="human:alex")
    participant_id = await ensure_participant(
        db, timeline_id=uuid4(), source_actor_id="human:alex",
        actor_type="user", controller_type="human",
    )
    insert = next(args for sql, args in db.statements
                  if "INSERT INTO aios.conversation_participant" in sql)
    assert insert[1] == "human:alex"
    assert insert[3] == "user"
    assert await bind_available_instance(db, participant_id=participant_id) is not None
    bind_sql = db.statements[-1][0]
    assert "cp.actor_type='character'" not in bind_sql


@pytest.mark.asyncio
async def test_user_display_name_does_not_claim_an_unrelated_identity():
    db = ParticipantDB()
    await ensure_participant(
        db, timeline_id=uuid4(), source_actor_id="Alex",
        actor_type="user", controller_type="human",
    )
    insert = next(args for sql, args in db.statements
                  if "INSERT INTO aios.conversation_participant" in sql)
    assert insert[1] is None


@pytest.mark.asyncio
async def test_reselection_does_not_preserve_binding_after_identity_change():
    db = ParticipantDB(identity="human:alex")
    await ensure_participant(
        db, timeline_id=uuid4(), source_actor_id="human:alex",
        actor_type="user",
    )
    insert_sql = next(sql for sql, _ in db.statements
                      if "INSERT INTO aios.conversation_participant" in sql)
    assert "character_id=EXCLUDED.character_id" in insert_sql
    assert "IS NOT DISTINCT FROM EXCLUDED.character_id" in insert_sql
