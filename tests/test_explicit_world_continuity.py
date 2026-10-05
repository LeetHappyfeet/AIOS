"""The transport session is not a shared-world or memory-continuity grant."""
from pathlib import Path
from uuid import uuid4

import pytest

from aios_app.world.topology import ensure_runtime_branch_world


class NewWorldDB:
    def __init__(self):
        self.keys = []

    async def fetchrow(self, query, *args):
        return None

    async def execute_returning_row(self, query, *args):
        key = args[0]
        self.keys.append(key)
        return {
            "world_id": uuid4(), "world_key": key, "world_type": "runtime",
            "parent_world_id": args[1], "root_world_id": args[1],
            "anchor_timeline_id": None, "anchor_node_id": None,
            "origin_character_id": args[2],
        }


@pytest.mark.asyncio
async def test_runtime_world_separates_persona_and_scope_even_with_reused_session():
    db = NewWorldDB()
    session_id, root = uuid4(), uuid4()
    kw = dict(character_id="Renamon", session_id=session_id, root_world_id=root)
    alex = await ensure_runtime_branch_world(db, user_name="Alex_", scope_key="conversation", **kw)
    george = await ensure_runtime_branch_world(
        db, user_name="George Constanza", scope_key="conversation", **kw,
    )
    alex_again = await ensure_runtime_branch_world(db, user_name="Alex_", scope_key="conversation", **kw)
    other_scope = await ensure_runtime_branch_world(db, user_name="Alex_", scope_key="notes", **kw)
    assert alex["world_key"] == alex_again["world_key"]
    assert len({alex["world_key"], george["world_key"], other_scope["world_key"]}) == 3
    assert "Alex_" not in alex["world_key"]


def test_immutable_migration_requires_explicit_compatible_world_history():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "current" /
           "20261004_21_explicit_world_cognitive_continuity.sql").read_text()
    assert "DROP TRIGGER IF EXISTS trg_link_runtime_world_continuity" in sql
    assert "COALESCE(meta->>'automatic','false')='true'" in sql
    assert "CREATE OR REPLACE FUNCTION aios.cognitive_evidence_instances" in sql
    assert "r.relation='continues'" in sql
    assert "ci.meta->>'runtime_user_name' IS NOT DISTINCT FROM t.runtime_user_name" in sql
    assert "PERFORM aios.reconcile_character_belief_atom" in sql


def test_fallback_uses_evidence_world_and_scope():
    import inspect
    from aios_app.epistemic.cognitive_context import CognitiveContextService
    from aios_app.epistemic.retrieval import TopologyRetriever
    fallback = inspect.getsource(CognitiveContextService._flat_character_knowledge)
    topology = inspect.getsource(TopologyRetriever.retrieve_character_knowledge)
    assert 'candidate_world_id=item.get("evidence_world_id") or context.world_id' in fallback
    assert 'else item.get("evidence_world_id") or context.world_id' in topology
