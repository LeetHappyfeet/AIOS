"""Bounded, auditable deterministic corrections; inference never auto-certifies evidence."""
from __future__ import annotations
import json
import re
from uuid import UUID
from aios_app.epistemic.semantic_integrity import validate_claim, IntegrityResult

async def repair_claim(db, *, claim_id: UUID, receipt: IntegrityResult) -> IntegrityResult:
    row = await db.fetchrow(
        """SELECT repair_attempts, revision_key, validator_version, status,
                  reason_codes, frame_snapshot FROM aios.claim_semantic_integrity
           WHERE claim_id=$1 FOR UPDATE""", claim_id)
    if not row or receipt.status == "valid" or row["repair_attempts"] >= 2:
        return receipt
    await db.execute(
        """INSERT INTO aios.claim_semantic_integrity_revision
             (claim_id, revision_key, validator_version, status, reason_codes, frame_snapshot)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb)
           ON CONFLICT DO NOTHING""",
        claim_id, row["revision_key"], row["validator_version"],
        row["status"], json.dumps(row["reason_codes"]),
        json.dumps(row["frame_snapshot"]),
    )
    await db.execute(
        """UPDATE aios.claim_semantic_integrity
           SET repair_attempts=repair_attempts+1, repair_state='deterministic_attempted'
           WHERE claim_id=$1""", claim_id)
    source = await db.fetchrow("SELECT source_text FROM aios.claim_semantic_integrity WHERE claim_id=$1", claim_id)
    text = source["source_text"] or ""
    frames = await db.fetch(
        """SELECT frame_id, subject_text, resolved_subject, object_text,
                  resolved_object, predicate_canonical, meta
           FROM aios.claim_semantic_frame WHERE claim_id=$1 ORDER BY frame_index""", claim_id)
    changed = False
    for frame in frames:
        meta = frame["meta"] if isinstance(frame["meta"], dict) else {}
        original_subject = str(meta.get("source_subject_text") or "").lower()
        original_object = str(meta.get("source_object_text") or "").lower()
        resolved_subject = str(frame["resolved_subject"] or "").lower()
        resolved_object = str(frame["resolved_object"] or "").lower()
        replacement_subject = None
        replacement_object = None
        if re.search(r"\bit(?:'s| is)\s+\w+\s+out\b", text.lower()) and resolved_subject not in {"outdoor conditions", "weather"}:
            replacement_subject = "outdoor conditions"
        elif original_subject in {"she", "her"} and resolved_subject in {"he", "him", "his"}:
            replacement_subject = original_subject
        elif original_subject in {"he", "him"} and resolved_subject in {"she", "her", "hers"}:
            replacement_subject = original_subject
        if original_object in {"him", "his"} and resolved_object in {"she", "her", "hers"}:
            replacement_object = original_object
        elif original_object in {"her", "hers"} and resolved_object in {"he", "him", "his"}:
            replacement_object = original_object
        if replacement_subject is not None or replacement_object is not None:
            await db.execute(
                """UPDATE aios.claim_semantic_frame
                   SET resolved_subject=COALESCE($2,resolved_subject),
                       resolved_object=COALESCE($3,resolved_object),
                       subject_entity_key=CASE WHEN $2 IS NOT NULL THEN NULL ELSE subject_entity_key END,
                       object_entity_key=CASE WHEN $3 IS NOT NULL THEN NULL ELSE object_entity_key END,
                       resolution_status='partial',
                       meta=COALESCE(meta,'{}'::jsonb) || '{"source_grounded_repair":true}'::jsonb
                   WHERE frame_id=$1""",
                frame["frame_id"], replacement_subject, replacement_object)
            changed = True
    if not changed:
        await db.execute("UPDATE aios.claim_semantic_integrity SET repair_state='needs_context' WHERE claim_id=$1", claim_id)
        return receipt
    result = await validate_claim(db, claim_id=claim_id)
    await db.execute(
        "UPDATE aios.claim_semantic_integrity SET repair_state=$2 WHERE claim_id=$1",
        claim_id, "corrected" if result.status=="valid" else "needs_context")
    return result
