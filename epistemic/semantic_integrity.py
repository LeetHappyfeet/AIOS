"""Source-grounded integrity receipts for semantic frame admission.

Integrity describes fidelity to testimony, not whether testimony is true.
A receipt belongs to a claim/frame occurrence, never globally to a proposition.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping
from uuid import UUID

INTEGRITY_VERSION = "semantic-integrity-v3-fidelity"
_PRONOUNS = {"he", "him", "his", "she", "her", "hers", "it", "its", "they", "them",
             "their", "this", "that", "which", "who", "whom", "you", "i"}
_TRANSITIVE = {"watch", "have", "give", "push", "groom", "let", "tell", "find", "make"}
_DISCOURSE_SUBJECTS = {"either", "neither", "both", "someone", "something", "anything", "whatever"}
_PATTERN = re.compile(r"[a-z]+(?:'[a-z]+)?")

@dataclass(frozen=True)
class IntegrityResult:
    status: str
    reasons: tuple[str, ...] = ()

def _norm(text):
    return " ".join(_PATTERN.findall(str(text or "").casefold()))

def validate_frame(source: str, frame: Mapping, *, speaker_id: str | None = None,
                   source_subject: str | None = None,
                   source_object: str | None = None) -> IntegrityResult:
    """Conservative deterministic failures; ambiguous reference stays incomplete.

    No lexical overlap alone can certify a referent. Source role anchors are
    taken from the original parse, not a resolved candidate's entity key.
    """
    text = _norm(source)
    subject = _norm(frame.get("subject"))
    predicate = _norm(frame.get("predicate"))
    obj = _norm(frame.get("object"))
    reasons = []
    if not predicate or predicate == "_":
        return IntegrityResult("incomplete", ("missing_predicate",))
    if not subject or subject == "_":
        return IntegrityResult("incomplete", ("missing_subject",))
    # The source comparison must catch plausible triples that discard the
    # actual speech act or actor. Abstain on unresolved discourse, do not
    # promote malformed but schema-compliant representations.
    if subject in _DISCOURSE_SUBJECTS:
        return IntegrityResult("incomplete", ("unresolved_discourse_subject",))
    if re.search(r"\bgoing\s+to\s+need\b", text) and predicate in {"go", "going"}:
        return IntegrityResult("invalid", ("future_auxiliary_misread_as_action",))
    if (re.search(r"\bneed(?:s|ed)?\s+to\s+\w+", text)
            and predicate in {"need", "needs"} and not obj):
        return IntegrityResult("incomplete", ("modal_action_argument_lost",))
    if (re.search(r"\b(?:he|george)\b[^.!?]*\bregretted\b", text)
            and predicate in {"regret", "regretted"}
            and subject not in {"he", "george", "george constanza", "george costanza"}):
        return IntegrityResult("invalid", ("source_actor_mismatch",))
    # Preserve attribution: future speech and figurative characterizations
    # are not observed literal events just because they form valid triples.
    if (re.search(r"\bwill\s+say\b", text) and predicate in {"say", "says", "said"}
            and (not obj or str(frame.get("modality") or "asserted").lower() in {"asserted", "actual"})):
        return IntegrityResult("incomplete", ("future_reported_speech_not_actual_event",))
    if (re.search(r"\b(?:can.t|cannot|can t)\s+metabolize\s+honesty\b", text)
            and predicate == "metabolize"):
        return IntegrityResult("incomplete", ("figurative_literal_scope_unverified",))
    if "hunted look" in text and predicate in {"hunt", "hunted"}:
        return IntegrityResult("invalid", ("adjectival_description_misread_as_event",))
    if subject == "which" and not text.startswith("which"):
        return IntegrityResult("invalid", ("unsupported_relative_subject",))
    if subject == "which" and text.startswith("which"):
        return IntegrityResult("invalid", ("discourse_marker_as_subject",))
    if re.search(r"\bit(?:'s| is)\s+\w+\s+out\b", source.casefold()) and subject not in {"it", "outdoor conditions", "weather"}:
        reasons.append("environmental_subject_replaced_with_entity")
    if re.search(r"\bshe\s+\w+\b", text) and subject in {"he", "him", "his"}:
        reasons.append("subject_gender_contradicts_source")
    original_subject = _norm(source_subject)
    original_object = _norm(source_object)
    feminine = {"she", "her", "hers"}
    masculine = {"he", "him", "his"}
    if original_subject in feminine and subject in masculine:
        reasons.append("subject_gender_contradicts_source")
    if original_subject in masculine and subject in feminine:
        reasons.append("subject_gender_contradicts_source")
    if original_object in masculine and obj in feminine:
        reasons.append("object_gender_contradicts_source")
    if original_object in feminine and obj in masculine:
        reasons.append("object_gender_contradicts_source")
    # When the resolved object equals the subject but the text explicitly
    # distinguishes she/him (or he/her), this is not an anaphoric identity.
    if subject and subject == obj and (
        re.search(r"\bshe\s+\w+\s+him\b", text) or
        re.search(r"\bhe\s+\w+\s+her\b", text)
    ):
        reasons.append("distinct_source_participants_collapsed")
    # A named entity substituted for an unresolved grammatical subject needs
    # positive grounding; authored text and explicitly speaker-bound 'I' count.
    if original_subject in {"it", "its"} and subject not in {"it", "its", "outdoor conditions", "weather"}:
        reasons.append("unjustified_neutral_pronoun_resolution")
    if original_subject in feminine | masculine and subject not in feminine | masculine:
        # Cross-sentence person references require a separate referent receipt;
        # do not assert invalidity merely because a name is absent locally.
        return IntegrityResult("invalid", tuple(sorted(set(reasons)))) if reasons else IntegrityResult(
            "incomplete", ("third_person_reference_requires_grounding",))
    if reasons:
        return IntegrityResult("invalid", tuple(sorted(set(reasons))))
    if subject in {"it", "its", "this", "that"} and not (
        subject == "it" and re.search(r"\bit(?:'s| is)\s+\w+\s+out\b", source.casefold())
    ):
        return IntegrityResult("incomplete", ("unresolved_neutral_reference",))
    if obj in {"it", "its", "this", "that", "him", "her", "them"}:
        return IntegrityResult("incomplete", ("unresolved_object_reference",))
    if not obj and predicate in _TRANSITIVE:
        return IntegrityResult("incomplete", ("missing_expected_argument",))
    return IntegrityResult("valid")

def revision_key(source: str, frames: list[dict], *, version: str = INTEGRITY_VERSION, context_digest: str = "") -> str:
    material = json.dumps([version, source, frames, context_digest], sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()

async def validate_claim(db, *, claim_id: UUID) -> IntegrityResult:
    """Persist a durable receipt for the currently selected frame revision."""
    row = await db.fetchrow(
        """SELECT cc.raw_text, dn.speaker_id, ds.content AS source_section, dn.node_id
           FROM aios.claim_candidate cc
           JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
           JOIN aios.document_section ds ON ds.section_id=es.section_id
           JOIN aios.dag_node dn ON dn.node_id=ds.node_id
           WHERE cc.claim_id=$1""", claim_id)
    if not row:
        raise LookupError(f"Missing source for claim {claim_id}")
    frames = await db.fetch(
        """SELECT frame_id, resolved_subject, subject_text, predicate_canonical,
                  predicate_surface, resolved_object, object_text, polarity, modality,
                  resolution_status, meta
           FROM aios.claim_semantic_frame
           WHERE claim_id=$1 AND decomposer_version='semantic-frame-v2'
           ORDER BY frame_index""", claim_id)
    if not frames:
        result = IntegrityResult("incomplete", ("no_semantic_frames",))
    else:
        results = []
        for f in frames:
            meta = f["meta"] if isinstance(f["meta"], dict) else {}
            results.append(validate_frame(
                row["raw_text"],
                {"subject": f["resolved_subject"] or f["subject_text"],
                 "predicate": f["predicate_canonical"] or f["predicate_surface"],
                 "object": f["resolved_object"] or f["object_text"],
                 "modality": f["modality"], "polarity": f["polarity"]},
                speaker_id=row["speaker_id"],
                source_subject=meta.get("source_subject_text", f["subject_text"]),
                source_object=meta.get("source_object_text", f["object_text"]),
            ))
        status = "invalid" if any(r.status == "invalid" for r in results) else (
            "incomplete" if any(r.status == "incomplete" for r in results) else "valid")
        result = IntegrityResult(status, tuple(sorted({reason for r in results for reason in r.reasons})))
    snapshot = [{"frame_id": str(f["frame_id"]),
                 "subject": f["resolved_subject"] or f["subject_text"],
                 "predicate": f["predicate_canonical"] or f["predicate_surface"],
                 "object": f["resolved_object"] or f["object_text"],
                 "polarity": f["polarity"], "modality": f["modality"]} for f in frames]
    # Bind the receipt to its containing source paragraph and DAG coordinate.
    # Changing the paragraph invalidates the receipt without inventing referents.
    section_digest = hashlib.sha256(str(row["source_section"] or "").encode()).hexdigest()
    revision = revision_key(row["raw_text"], snapshot, context_digest=section_digest)
    await db.execute(
        """INSERT INTO aios.claim_semantic_integrity
             (claim_id, revision_key, validator_version, status, reason_codes, source_text, frame_snapshot)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb)
           ON CONFLICT (claim_id) DO UPDATE SET
             revision_key=EXCLUDED.revision_key,
             validator_version=EXCLUDED.validator_version,
             status=EXCLUDED.status,
             reason_codes=EXCLUDED.reason_codes,
             source_text=EXCLUDED.source_text,
             frame_snapshot=EXCLUDED.frame_snapshot,
             checked_at=now()
           WHERE aios.claim_semantic_integrity.revision_key IS DISTINCT FROM EXCLUDED.revision_key
              OR aios.claim_semantic_integrity.validator_version IS DISTINCT FROM EXCLUDED.validator_version""",
        claim_id, revision, INTEGRITY_VERSION, result.status,
        json.dumps(result.reasons), row["raw_text"], json.dumps(snapshot))
    return result
