"""Canonical AIOS ASGI entrypoint.

The established API surface lives in main_legacy. POST /ingest is replaced by
the idempotent implementation so exact active SillyTavern replays return before
DAG, runtime-cursor, HUD-dirty, and downstream pipeline side effects.
"""

from aios_app.main_legacy import app, db
from aios_app.ingest_api import ingest_message
from aios_app.models import IngestIn, IngestOut
from aios_app.char.identity_bootstrap import bootstrap_character_card
from aios_app.char.identity_revision import accept_identity_candidate, reject_identity_candidate
from pydantic import BaseModel
from typing import Any


app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == "/ingest"
        and "POST" in (getattr(route, "methods", None) or set())
    )
]


@app.post("/ingest", response_model=IngestOut)
async def ingest(req: IngestIn) -> IngestOut:
    return await ingest_message(db, req)


class CharacterCardBootstrapIn(BaseModel):
    card: dict[str, Any]
    source_name: str | None = None
    source_format: str = "character_card"
    replace_authored_facets: bool = True
    auto_accept_authored: bool = True


@app.post("/character/{character_id}/identity/bootstrap/card")
async def bootstrap_identity_card(
    character_id: str,
    req: CharacterCardBootstrapIn,
) -> dict[str, Any]:
    """Explicitly bootstrap slow-changing identity from an external card."""
    return await bootstrap_character_card(
        db,
        character_id=character_id,
        payload=req.card,
        source_name=req.source_name,
        source_format=req.source_format,
        replace_authored_facets=req.replace_authored_facets,
        auto_accept_authored=req.auto_accept_authored,
    )


class IdentityCandidateDecisionIn(BaseModel):
    reason: str | None = None
    actor: str = "api"


@app.post("/character/{character_id}/identity/candidate/{candidate_id}/accept")
async def accept_character_identity_candidate(
    character_id: str,
    candidate_id: str,
    req: IdentityCandidateDecisionIn,
) -> dict[str, Any]:
    candidate = await db.fetchrow(
        "SELECT character_id FROM aios.character_identity_candidate WHERE candidate_id=$1::uuid",
        candidate_id,
    )
    if not candidate or candidate["character_id"] != character_id:
        raise LookupError("identity candidate does not belong to character")
    return await accept_identity_candidate(
        db, candidate_id, actor=req.actor, reason=req.reason
    )


@app.post("/character/{character_id}/identity/candidate/{candidate_id}/reject")
async def reject_character_identity_candidate(
    character_id: str,
    candidate_id: str,
    req: IdentityCandidateDecisionIn,
) -> dict[str, Any]:
    candidate = await db.fetchrow(
        "SELECT character_id FROM aios.character_identity_candidate WHERE candidate_id=$1::uuid",
        candidate_id,
    )
    if not candidate or candidate["character_id"] != character_id:
        raise LookupError("identity candidate does not belong to character")
    await reject_identity_candidate(db, candidate_id, reason=req.reason)
    return {"character_id": character_id, "candidate_id": candidate_id, "disposition": "rejected"}
