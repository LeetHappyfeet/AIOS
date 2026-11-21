"""Discovery bridge regression tests with a deterministic fake database."""
import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aios_app.agent.research_discovery import ResearchNominationIn, nominate


INSTANCE, FOREIGN, DOSSIER, BINDING, RUN, CLAIM = [uuid4() for _ in range(6)]


class DB:
    def __init__(self, *, owner=INSTANCE, status="open"):
        self.owner, self.status = owner, status
        self.inserted = {}
    @asynccontextmanager
    async def atomic(self):
        yield self
    async def fetchrow(self, sql, *args):
        if "FROM aios.character_research_dossier" in sql:
            if args != (DOSSIER, self.owner):
                return None
            return {"dossier_id": DOSSIER, "status": self.status}
        if "INSERT INTO aios.research_external_nomination" in sql:
            key = (args[3], args[4])
            if key in self.inserted:
                return None
            row = {"nomination_id": uuid4(), "payload_sha256": args[-1]}
            self.inserted[key] = row
            return row
        if "FROM aios.research_external_nomination" in sql:
            return self.inserted.get(tuple(args))
        raise AssertionError(sql)


class Store:
    async def owned(self, binding_id, run_id, req):
        assert (binding_id, run_id) == (BINDING, RUN)
        assert (req.worker_key, req.claim_token) == ("research-worker", CLAIM)
        return ({"instance_id": FOREIGN}, {
            "origin_instance_id": INSTANCE, "required_protocol_version": 3})


def request(**overrides):
    values = {
        "worker_key": "research-worker", "claim_token": CLAIM,
        "dossier_id": DOSSIER, "evidence_id": "a" * 24,
        "source_id": "library", "kind": "calibre_book",
        "title": "Titan", "content_scope": "retrieved_passage",
        "document_sha256": "b" * 64,
        "location": {"catalog_id": "42", "path": "/private/books/secret.pdf"},
        "reason": "Evidence for the dossier",
    }
    values.update(overrides)
    return ResearchNominationIn(**values)


def run(db, req):
    return asyncio.run(nominate(db, Store(), binding_id=BINDING, run_id=RUN, req=req))


def test_nomination_is_idempotent_and_never_carries_raw_text():
    db = DB()
    first = run(db, request())
    second = run(db, request())
    assert first["nomination_id"] == second["nomination_id"]
    assert not first["corpus_imported"] and not first["character_knowledge_acquired"]
    assert len(db.inserted) == 1


def test_other_instance_or_closed_dossier_is_rejected():
    with pytest.raises(HTTPException) as error:
        run(DB(owner=FOREIGN), request())
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        run(DB(status="closed"), request())
    assert error.value.status_code == 409


def test_changed_evidence_retry_is_rejected():
    db = DB()
    run(db, request())
    with pytest.raises(HTTPException) as error:
        run(db, request(reason="Changed justification"))
    assert error.value.status_code == 409


def test_local_secret_paths_and_private_urls_not_accepted():
    assert request().location["path"].startswith("/private/")
    with pytest.raises(HTTPException) as error:
        run(DB(), request(public_url="http://192.168.1.1/secret"))
    assert error.value.status_code == 422
