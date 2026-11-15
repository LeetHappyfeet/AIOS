"""Research source nominations from leased AIOS-Client runs.

Nominations preserve retrieval provenance only. Nothing here imports corpus
text, updates RDF, records character experience, or asserts factual truth.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ResearchNominationIn(BaseModel):
    worker_key: str = Field(min_length=1, max_length=160)
    claim_token: UUID
    dossier_id: UUID
    evidence_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: Literal["web_search_result", "calibre_book", "local_document"]
    title: str = Field(min_length=1, max_length=200)
    content_scope: Literal["retrieved_passage"]
    document_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    section: str | None = Field(default=None, max_length=400)
    retrieved_at: datetime | None = None
    public_url: str | None = Field(default=None, max_length=512)
    location: str | dict[str, Any] | None = None
    reason: str = Field(min_length=1, max_length=600)


async def nominate(db, store, *, binding_id: UUID, run_id: UUID,
                   req: ResearchNominationIn) -> dict[str, Any]:
    # Worker's lease and originating character are authoritative. The request
    # cannot choose an arbitrary instance, character, workspace or scope.
    async with db.atomic():
        binding, run = await store.owned(binding_id, run_id, req)
        if run["required_protocol_version"] != 3:
            raise HTTPException(409, "Research nomination requires a v3 leased run")
        instance_id = run["origin_instance_id"] or binding["instance_id"]
        dossier = await db.fetchrow(
            """SELECT dossier_id,status FROM aios.character_research_dossier
               WHERE dossier_id=$1 AND instance_id=$2 FOR SHARE""",
            req.dossier_id, instance_id,
        )
        if not dossier:
            raise HTTPException(403, "Dossier is not owned by the originating character")
        if dossier["status"] != "open":
            raise HTTPException(409, "Research dossier is not open")

        reference = {}
        if req.public_url:
            # Private links and local identifiers remain private to the client.
            from urllib.parse import urlsplit
            import ipaddress
            try:
                parsed = urlsplit(req.public_url)
                host = (parsed.hostname or "").lower()
                try:
                    is_global = ipaddress.ip_address(host).is_global
                except ValueError:
                    is_global = bool(host) and not host.endswith(
                        (".local", ".lan", ".internal", ".test"))
                if (parsed.scheme != "https" or not is_global or parsed.username or
                        parsed.password or parsed.query or parsed.fragment):
                    raise ValueError("Nonpublic URL")
            except ValueError:
                raise HTTPException(422, "Only public HTTPS references may be transmitted")
            reference["public_url"] = req.public_url
        # Avoid saving private library filesystem paths in server-side reports.
        if isinstance(req.location, dict) and isinstance(req.location.get("catalog_id"), str):
            reference["catalog_id"] = req.location["catalog_id"][:300]

        canonical = {
            "dossier_id": str(req.dossier_id),
            "evidence_id": req.evidence_id,
            "source_id": req.source_id,
            "kind": req.kind,
            "title": req.title,
            "document_sha256": req.document_sha256,
            "section": req.section,
            "retrieved_at": req.retrieved_at.isoformat() if req.retrieved_at else None,
            "reference": reference,
            "reason": req.reason,
        }
        fingerprint = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        row = await db.fetchrow(
            """INSERT INTO aios.research_external_nomination
               (dossier_id,instance_id,binding_id,run_id,evidence_id,source_id,
                kind,title,source_reference,document_sha256,section,rationale,
                source_retrieved_at,payload_sha256)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14)
               ON CONFLICT(run_id,evidence_id) DO NOTHING
               RETURNING nomination_id,payload_sha256,created_at""",
            req.dossier_id, instance_id, binding_id, run_id, req.evidence_id,
            req.source_id, req.kind, req.title, json.dumps(reference),
            req.document_sha256, req.section, req.reason, req.retrieved_at,
            fingerprint,
        )
        if row is None:
            row = await db.fetchrow(
                """SELECT nomination_id,payload_sha256,created_at
                   FROM aios.research_external_nomination
                   WHERE run_id=$1 AND evidence_id=$2""",
                run_id, req.evidence_id,
            )
            if row is None or row["payload_sha256"] != fingerprint:
                raise HTTPException(409, "Nomination retry changed its evidence")
        return {
            "nomination_id": str(row["nomination_id"]),
            "dossier_id": str(req.dossier_id),
            "evidence_id": req.evidence_id,
            "status": "nominated",
            "corpus_imported": False,
            "character_knowledge_acquired": False,
        }
