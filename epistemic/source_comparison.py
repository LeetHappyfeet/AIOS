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
        return section[: 2 * radius]
    lo = max(0, anchor - radius)
    hi = min(len(section), anchor + len(sentence) + radius)
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
        "evidence_quote": str(raw.get("evidence_quote") or "")[:300],
        "proposed_correction": raw.get("proposed_correction")
        if isinstance(raw.get("proposed_correction"), dict) else None,
        "admission_effect": "none",
    }
