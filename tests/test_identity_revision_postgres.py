"""Verify identity candidate decisions against PostgreSQL parameter inference."""
import json

import pytest

from aios_app.char.identity_revision import reject_identity_candidate
from aios_app.char.identity_sources import stage_identity_source
from test_cognitive_episode_postgres import ROOT, db, pytestmark  # noqa: F401


@pytest.mark.asyncio
async def test_reject_candidate_accepts_optional_reason_and_only_changes_proposals(db):
    await db.execute("CREATE TABLE aios.character_identity(character_id text PRIMARY KEY)")
    await db.pool.con.query(
        (ROOT / "migrations/current/20260919_identity_kernel.sql").read_text(), exec=True
    )
    await db.execute("INSERT INTO aios.character_identity(character_id) VALUES('TestCharacter')")
    staged = await stage_identity_source(
        db, character_id="TestCharacter", source_type="manual",
        payload={"description": "candidate descriptions"},
        candidates=[
            {"facet_type": "description", "facet_key": key, "value": key, "meta": {"keep": True}}
            for key in ("no_reason", "with_reason", "already_accepted")
        ],
    )
    no_reason, with_reason, accepted = staged["candidate_ids"]
    await db.execute(
        "UPDATE aios.character_identity_candidate SET disposition='accepted' WHERE candidate_id=$1::uuid",
        accepted,
    )

    await reject_identity_candidate(db, no_reason)
    await reject_identity_candidate(db, with_reason, reason="Incorrect description")
    with pytest.raises(LookupError, match="no proposed identity candidate"):
        await reject_identity_candidate(db, accepted, reason="Would require a replacement")

    rows = await db.fetch(
        """SELECT facet_key,disposition,decided_at,meta FROM aios.character_identity_candidate"""
    )
    by_key = {row["facet_key"]: row for row in rows}
    for key, reason in (("no_reason", None), ("with_reason", "Incorrect description")):
        row = by_key[key]
        assert row["disposition"] == "rejected"
        assert row["decided_at"] is not None
        meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
        assert meta == {"keep": True, "rejection_reason": reason}
    assert by_key["already_accepted"]["disposition"] == "accepted"
