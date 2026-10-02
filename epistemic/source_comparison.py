"""Read-only source-DAG semantic comparison using the optional inference broker.

This worker proposes discrepancies for review; model output never certifies an
integrity receipt, repairs frames, or mutates character knowledge. The admission
authority remains semantic_integrity.validate_claim().
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from aios_app.inference import InferenceBroker, InferenceRequest


_ALLOWED = {"supported", "incomplete", "contradicted", "ambiguous"}


def bounded_source_context(section: str, sentence: str, *, radius: int = 900) -> str:
    """Limit inference to the original section, near the actual source span."""
    section = str(section or "")
    sentence = str(sentence or "").strip()
    anchor = section.find(sentence) if sentence else -1
    if anchor < 0:
        # If the sentence is not located, never include unknown later material.
        return sentence[: 2 * radius]
    lo = max(0, anchor - radius)
    # Never include later section text in a historical grounding window.
    hi = min(len(section), anchor + len(sentence))
    return section[lo:hi]


def build_comparison_prompt(*, source: str, section: str, speaker: str | None,
                            recipient: str | None, frames: list[dict]) -> str:
    """Request a discrepancy audit, never a free-form memory reconstruction."""
    payload = {
        "source_sentence": source,
        "source_context": bounded_source_context(section, source),
        "speaker": speaker,
        "recipient": recipient,
        "candidate_frames": frames,
    }
    return (
        "Compare candidate semantic frames ONLY to the supplied original source "
        "and its immediate section context. The source is testimony, not necessarily "
        "objective truth. Distinguish speaker, actor, addressee, desire/obligation "
        "owner, performed versus requested action, polarity, modality, and missing "
        "semantic arguments. Do not use later events or character memory. "
        "Do not infer that a request addressed to a character is a goal adopted "
        "by that character. Do not invent missing referents. Return JSON with "
        "verdict (supported|incomplete|contradicted|ambiguous), reason_codes "
        "(array of short strings), evidence_quote (short exact source span), "
        "and proposed_correction (object or null). A proposal is NOT approval "
        "for database mutation.\n" + json.dumps(payload, ensure_ascii=False, default=str)
    )


async def compare_claim_with_local_inference(db, *, claim_id: UUID,
                                             instance_id: UUID) -> dict:
    """Optional, bounded diagnostic operation; never called during admission."""
    source = await db.fetchrow(
        """SELECT cc.raw_text, ds.content AS source_section, dn.node_id,
                  dn.speaker_id, dn.recipient_id
           FROM aios.claim_candidate cc
           JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
           JOIN aios.document_section ds ON ds.section_id=es.section_id
           JOIN aios.dag_node dn ON dn.node_id=ds.node_id
           WHERE cc.claim_id=$1""", claim_id,
    )
    if not source:
        raise LookupError(f"Missing DAG source for claim {claim_id}")
    rows = await db.fetch(
        """SELECT frame_index, resolved_subject, subject_text, predicate_canonical,
                  predicate_surface, resolved_object, object_text, polarity, modality
           FROM aios.claim_semantic_frame
           WHERE claim_id=$1 AND decomposer_version='semantic-frame-v2'
           ORDER BY frame_index""", claim_id,
    )
    frames = [
        {"index": row["frame_index"],
         "subject": row["resolved_subject"] or row["subject_text"],
         "predicate": row["predicate_canonical"] or row["predicate_surface"],
         "object": row["resolved_object"] or row["object_text"],
         "polarity": row["polarity"], "modality": row["modality"]}
        for row in rows
    ]
    context = str(source["source_section"] or "")
    prompt = build_comparison_prompt(
        source=str(source["raw_text"] or ""), section=context,
        speaker=source["speaker_id"], recipient=source["recipient_id"],
        frames=frames,
    )
    result = await InferenceBroker(db).infer(InferenceRequest(
        instance_id=instance_id, worker_class="message_cognition",
        prompt=prompt, allowed_actions={}, output_schema={"type": "object"},
        temperature=0.0, max_tokens=500,
    ))
    raw = result.response.raw
    raw = raw if isinstance(raw, dict) else {}
    verdict = str(raw.get("verdict") or "").lower()
    if verdict not in _ALLOWED:
        verdict = "ambiguous"
    # An alleged evidence quote must be traceable to the supplied source.
    quote = str(raw.get("evidence_quote") or "")[:300]
    if not quote or (quote not in str(source["raw_text"] or "") and quote not in bounded_source_context(context, str(source["raw_text"] or ""))):
        quote = ""
        verdict = "ambiguous"
    return {
        "claim_id": str(claim_id),
        "source_node_id": str(source["node_id"]),
        "source_section_sha256": hashlib.sha256(context.encode()).hexdigest(),
        "inference_request_id": str(result.request_id),
        "verdict": verdict,
        "reason_codes": [
            str(code)[:80] for code in (raw.get("reason_codes") or [])
            if isinstance(code, str)
        ][:8] if isinstance(raw.get("reason_codes"), list) else [],
        "evidence_quote": quote,
        "proposed_correction": raw.get("proposed_correction")
        if isinstance(raw.get("proposed_correction"), dict) else None,
        "admission_effect": "none",
        "verification_version": "source-compare-v2-preceding-context",
    }


async def audit_claim_with_local_inference(db, *, claim_id: UUID,
                                          instance_id: UUID) -> dict:
    """Persist audit against the exact integrity revision without admission effects."""
    before = await db.fetchrow(
        "SELECT revision_key FROM aios.claim_semantic_integrity WHERE claim_id=$1",
        claim_id,
    )
    if not before:
        raise LookupError(f"Missing integrity revision for claim {claim_id}")
    review = await compare_claim_with_local_inference(
        db, claim_id=claim_id, instance_id=instance_id,
    )
    revision = str(before["revision_key"])
    record = await db.fetchrow(
        """INSERT INTO aios.claim_source_comparison_audit (
             claim_id, revision_key, comparison_version, instance_id,
             inference_request_id, verdict, audit_json)
           SELECT $1, si.revision_key, $3, $4, $5, $6, $7::jsonb
           FROM aios.claim_semantic_integrity si
           WHERE si.claim_id=$1 AND si.revision_key=$2
           ON CONFLICT (claim_id, revision_key) DO UPDATE
           SET comparison_version=EXCLUDED.comparison_version,
               inference_request_id=EXCLUDED.inference_request_id,
               verdict=EXCLUDED.verdict,
               audit_json=EXCLUDED.audit_json,
               audited_at=now()
           RETURNING claim_id""",
        claim_id, revision, review["verification_version"], instance_id,
        review["inference_request_id"], review["verdict"], json.dumps(review),
    )
    if not record:
        raise RuntimeError("Source/frame revision changed during audit; retry")
    return {**review, "integrity_revision_key": revision, "review_persisted": True}



async def _audit_cli() -> None:
    """Explicit, bounded offline diagnostic; never part of ingestion hot path."""
    import argparse
    from aios_app.config import settings
    from aios_app.db import Database

    parser = argparse.ArgumentParser(description="Audit stored claims against source DAG")
    parser.add_argument("--instance-id", type=UUID, required=True)
    parser.add_argument("--claim-id", type=UUID, action="append", required=True)
    args = parser.parse_args()
    if len(args.claim_id) > 12:
        parser.error("At most 12 explicit claim IDs per invocation")
    db = Database(settings.db_dsn, min_size=1, max_size=2)
    await db.connect()
    try:
        for claim_id in dict.fromkeys(args.claim_id):
            review = await audit_claim_with_local_inference(
                db, claim_id=claim_id, instance_id=args.instance_id,
            )
            print(json.dumps(review, default=str, sort_keys=True))
    finally:
        await db.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_audit_cli())
