"""Store worker-attested research discoveries without importing or acquiring knowledge.

This is a discovery-only bridge from a leased workspace run. All source fields
are untrusted worker claims; no corpus ACL, ingestion, belief or world writes occur.
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

EVIDENCE_ID = re.compile(r"^[a-f0-9]{24}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
KINDS = frozenset({"web_search_result", "local_document", "calibre_book"})


def safe_public_url(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512 or not value.isprintable():
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or not host or parts.username or parts.password
                or parts.query or parts.fragment or host == "localhost"
                or host.endswith((".local", ".lan", ".internal", ".test"))):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return value
    except ValueError:
        return None


def validate_nominations(result):
    """Only allow metadata supported by citations from a structured worker report."""
    summary = result.get("result_summary")
    if not isinstance(summary, dict) or summary.get("source") != "publish_result":
        return None, []
    raw = summary.get("source_nominations")
    if not isinstance(raw, list) or not raw:
        return None, []
    if len(raw) > 8:
        return None, []
    citations = summary.get("research_citations", [])
    if not isinstance(citations, list):
        return None, []
    referenced = {
        c.get("evidence_id") for c in citations if isinstance(c, dict)
        and isinstance(c.get("finding_index"), int)
        and c.get("verification") == "source_retrieved_not_claim_verified"
    }
    dossier_id = summary.get("research_dossier_id")
    if dossier_id is not None:
        try:
            dossier_id = UUID(str(dossier_id))
        except (ValueError, TypeError, AttributeError):
            return None, []
    clean, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        evid = item.get("evidence_id")
        source = item.get("source_id")
        title = item.get("title")
        digest = item.get("document_sha256")
        kind = item.get("kind")
        if (not isinstance(evid, str) or not EVIDENCE_ID.fullmatch(evid)
                or evid in seen or evid not in referenced
                or not isinstance(source, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", source)
                or kind not in KINDS or not isinstance(title, str)
                or not title.strip() or len(title) > 200 or not title.isprintable()
                or not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                or item.get("verification") != "worker_retrieved_not_claim_verified"):
            continue
        section = item.get("section")
        if section is not None and (not isinstance(section, str) or len(section) > 240):
            continue
        location = item.get("location")
        # A local path or Calibre catalog ID is private source metadata, not a public URL.
        if location is not None and len(json.dumps(location, ensure_ascii=False)) > 1000:
            continue
        seen.add(evid)
        clean.append({
            "evidence_id": evid, "source_id": source, "kind": kind,
            "title": title.strip(), "document_sha256": digest,
            "section": section, "location": location,
            "public_url": safe_public_url(item.get("public_url")),
        })
    return dossier_id, clean


async def register_run_discoveries(db, *, run_id, instance_id, result):
    """Validate actual dossier ownership and persist idempotent discovery metadata."""
    dossier_id, items = validate_nominations(result)
    if not items:
        return 0
    if dossier_id is not None:
        owner = await db.fetchval(
            "SELECT 1 FROM aios.character_research_dossier WHERE dossier_id=$1 AND instance_id=$2",
            dossier_id, instance_id,
        )
        if not owner:
            # Never link a source discovery to an unrelated character's dossier.
            dossier_id = None
    for item in items:
        await db.execute(
            """INSERT INTO aios.research_external_discovery
               (run_id, evidence_id, instance_id, dossier_id, source_id, source_kind,
                title, document_sha256, section, location, public_url)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
               ON CONFLICT (run_id,evidence_id) DO NOTHING""",
            run_id, item["evidence_id"], instance_id, dossier_id,
            item["source_id"], item["kind"], item["title"], item["document_sha256"],
            item["section"], json.dumps(item["location"]), item["public_url"],
        )
    return len(items)
