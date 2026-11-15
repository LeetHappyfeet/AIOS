"""Discovery results stay worker-attested, scoped and separate from corpus ingestion."""
import asyncio
from uuid import uuid4

from aios_app.agent.research_discovery import (
    register_run_discoveries, safe_public_url, validate_nominations,
)


EVIDENCE = "a" * 24


def report(*, dossier_id=None):
    return {"result_summary": {
        "source": "publish_result",
        "research_dossier_id": str(dossier_id) if dossier_id else None,
        "research_citations": [{
            "finding_index": 0, "evidence_id": EVIDENCE,
            "verification": "source_retrieved_not_claim_verified",
        }],
        "source_nominations": [{
            "evidence_id": EVIDENCE, "source_id": "library",
            "kind": "calibre_book", "title": "An example book",
            "document_sha256": "b" * 64, "section": "chapter-1",
            "location": {"catalog_id": "private-book"},
            "public_url": "http://calibre.lan/opds/book/123",
            "verification": "worker_retrieved_not_claim_verified",
        }],
    }}


def test_research_nomination_is_private_discovery():
    dossier, items = validate_nominations(report())
    assert dossier is None and len(items) == 1
    assert items[0]["public_url"] is None
    assert items[0]["location"] == {"catalog_id": "private-book"}
    assert safe_public_url("https://example.org/reference") == "https://example.org/reference"
    assert safe_public_url("https://127.0.0.1/private") is None


def test_unread_and_uncited_content_rejected():
    value = report()
    value["result_summary"]["research_citations"] = []
    assert validate_nominations(value)[1] == []
    value = report()
    value["result_summary"]["source"] = "final_response"
    assert validate_nominations(value)[1] == []
    value = report()
    value["result_summary"]["source_nominations"][0]["document_sha256"] = "bad"
    assert validate_nominations(value)[1] == []


class DB:
    def __init__(self, owned):
        self.owned = owned
        self.calls = []

    async def fetchval(self, *args):
        return 1 if self.owned else None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def test_dossier_link_requires_same_instance():
    dossier_id, instance_id, run_id = uuid4(), uuid4(), uuid4()
    db = DB(owned=False)
    n = asyncio.run(register_run_discoveries(
        db, run_id=run_id, instance_id=instance_id, result=report(dossier_id=dossier_id)))
    assert n == 1
    assert db.calls[0][1][3] is None
    db = DB(owned=True)
    asyncio.run(register_run_discoveries(
        db, run_id=run_id, instance_id=instance_id, result=report(dossier_id=dossier_id)))
    assert db.calls[0][1][3] == dossier_id
