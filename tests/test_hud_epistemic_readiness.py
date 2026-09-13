from uuid import uuid4

import pytest

from aios_app.epistemic.context_resolver import _resolve_exact_runtime_instance
from aios_app.hud.readiness import source_node_retrieval_ready


class RuntimeResolverDB:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.args = None

    async def fetch(self, sql, *args):
        self.sql = sql
        self.args = args
        return self.rows


@pytest.mark.asyncio
async def test_runtime_binding_requires_one_exact_source_identity():
    source_timeline_id = uuid4()
    instance_id = uuid4()
    db = RuntimeResolverDB([{"instance_id": instance_id}])

    resolved = await _resolve_exact_runtime_instance(
        db,
        character_id="Alex",
        source_timeline_id=source_timeline_id,
    )

    assert resolved == instance_id
    assert "rs.source_timeline_id=$2" in db.sql
    assert "rt.user_name IS NOT DISTINCT FROM st.user_name" in db.sql
    assert db.args == ("Alex", source_timeline_id)


@pytest.mark.asyncio
async def test_runtime_binding_refuses_ambiguous_candidates():
    db = RuntimeResolverDB([
        {"instance_id": uuid4()},
        {"instance_id": uuid4()},
    ])

    resolved = await _resolve_exact_runtime_instance(
        db,
        character_id="Alex",
        source_timeline_id=uuid4(),
    )

    assert resolved is None


class ReadinessDB:
    def __init__(self, *, section, counts):
        self.section = section
        self.counts = counts
        self.queries = []

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        if "SELECT ds.section_id, ds.claims_extracted_at" in sql:
            return self.section
        if "WITH runtime_identity AS" in sql:
            return self.counts
        raise AssertionError(f"unexpected query: {sql}")


@pytest.mark.asyncio
async def test_narrative_claims_do_not_require_character_acquisition():
    db = ReadinessDB(
        section={"section_id": uuid4(), "claims_extracted_at": object()},
        counts={
            "total": 33,
            "contextualized": 33,
            "normalized": 33,
            "character_required": 0,
            "character_ready": 0,
        },
    )

    assert await source_node_retrieval_ready(
        db,
        instance_id=uuid4(),
        node_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_character_claims_gate_only_on_character_projection():
    db = ReadinessDB(
        section={"section_id": uuid4(), "claims_extracted_at": object()},
        counts={
            "total": 33,
            "contextualized": 33,
            "normalized": 33,
            "character_required": 2,
            "character_ready": 1,
        },
    )

    assert not await source_node_retrieval_ready(
        db,
        instance_id=uuid4(),
        node_id=uuid4(),
    )
