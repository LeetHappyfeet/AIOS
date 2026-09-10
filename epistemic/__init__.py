"""Epistemic normalization, narratives, knowledge projection, and gap-fill control."""

from .normalizer import normalize_claim_once
from .narratives import assign_narratives_once
from .knowledge import project_knowledge_acquisitions_once, record_acquisition
from . import generated as _generated
from .generated import create_generated_fact
from .generated_validated import resolve_generated_facts_validated
from .entity_validator import resolve_character_referent
from .hypothesis_validation import MatrixOutcome, record_validation_decision

from . import topology as _topology
from . import topology_claims as _topology_claims
from .ownership import resolve_semantic_ownership as _resolve_semantic_ownership
from .ownership_verifier import verify_semantic_ownership as _verify_semantic_ownership


def _matrix_outcome_from_ownership(result):
    verification = dict(result.evidence.get("adversarial_verification") or {})
    return MatrixOutcome(
        proposed_key=str(verification.get("proposed_owner_key") or result.owner_key),
        winner_key=verification.get("winner_owner_key") or result.owner_key,
        winner_score=int(verification.get("winner_score") or 0),
        runner_up_score=int(verification.get("runner_up_score") or 0),
        margin=int(verification.get("margin") or 0),
        stability=float(verification.get("stability") if verification.get("stability") is not None else 1.0),
        status=str(verification.get("status") or ("verified" if result.status == "resolved" else "insufficient_evidence")),
        matrix=dict(verification.get("matrix") or {}),
    )


async def _resolve_and_verify_semantic_ownership(db, row):
    proposed = await _resolve_semantic_ownership(db, row)
    result = await _verify_semantic_ownership(db, row, proposed)
    claim_id = row.get("claim_id")
    if claim_id:
        outcome = _matrix_outcome_from_ownership(result)
        dependencies = [
            ("claim_context", str(claim_id)),
            ("proposition", str(row.get("proposition_id") or "")),
        ]
        if row.get("world_id"):
            dependencies.append(("world", str(row["world_id"])))
        if row.get("speaker_id"):
            dependencies.append(("speaker_identity", str(row["speaker_id"]).strip().lower()))
        if row.get("origin_character_id"):
            dependencies.append(("character_identity", str(row["origin_character_id"]).strip().lower()))
        if row.get("proposition_id"):
            dependencies.append(("semantic_neighbors", str(row["proposition_id"])))

        await record_validation_decision(
            db,
            decision_type="semantic_owner",
            decision_key=str(claim_id),
            subject_type="claim",
            subject_key=str(claim_id),
            outcome=outcome,
            selected_value=result.owner_key if result.status == "resolved" else None,
            resolver_version="semantic-owner-recursive-v1",
            dependencies=dependencies,
            meta={"resolution_source": result.resolution_source},
        )

        if row.get("world_id"):
            await record_validation_decision(
                db,
                decision_type="world_assignment",
                decision_key=str(claim_id),
                subject_type="claim",
                subject_key=str(claim_id),
                outcome=outcome,
                selected_value=(str(row["world_id"]) if result.owner_kind == "world" else None),
                resolver_version="world-assignment-recursive-v1",
                dependencies=[
                    ("decision", f"semantic_owner:{claim_id}"),
                    ("world", str(row["world_id"])),
                    ("claim_context", str(claim_id)),
                ],
                meta={"owner_kind": result.owner_kind},
            )
    return result


async def _validated_character_mention(db, mention):
    return await resolve_character_referent(db, mention)


# Claim topology remains a materialized view of semantic decisions. The
# ownership/referent decisions are now independently revisable and can mark
# topology stale when their evidence changes.
_topology_claims.resolve_semantic_ownership = _resolve_and_verify_semantic_ownership
_topology_claims.resolve_character_mention = _validated_character_mention
_topology.choose_observation_scope = _topology_claims.choose_observation_scope
_topology.derive_claim_topology = _topology_claims.derive_claim_topology

# Preserve the existing runner import path while replacing the fragile
# generated-fact promotion rule with the adversarial promotion matrix.
_generated.resolve_generated_facts_once = resolve_generated_facts_validated
resolve_generated_facts_once = resolve_generated_facts_validated

__all__ = [
    "normalize_claim_once",
    "assign_narratives_once",
    "project_knowledge_acquisitions_once",
    "record_acquisition",
    "create_generated_fact",
    "resolve_generated_facts_once",
]
