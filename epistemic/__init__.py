"""Epistemic normalization, narratives, knowledge projection, and gap-fill control."""

from contextvars import ContextVar

from .normalizer import normalize_claim_once
from .narratives import assign_narratives_once
from .knowledge import project_knowledge_acquisitions_once, record_acquisition
from . import generated as _generated
from .generated import create_generated_fact
from .generated_validated import resolve_generated_facts_validated
from .entity_validator import resolve_character_referent
from .hypothesis_validation import MatrixOutcome, record_validation_decision
from .reality import REALITY_RESOLVER_VERSION, resolve_claim_reality

from . import topology as _topology
from . import topology_claims as _topology_claims
from .topology_projection import install_deferred_projection
from .ownership import resolve_semantic_ownership as _resolve_semantic_ownership
from .ownership_verifier import verify_semantic_ownership as _verify_semantic_ownership

_claim_context = ContextVar("aios_semantic_claim_context", default=None)


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


def _matrix_outcome_from_reality(reality, *, require_concrete_world: bool = False):
    memberships = list(reality.memberships if reality else ())
    matrix = {}
    for item in memberships:
        context_key = str(item["context_key"])
        matrix[context_key] = {
            "affinity": int(round(float(item["affinity"]) * 5)),
            "confidence": int(round(float(item["confidence"]) * 5)),
            "established_world": 3 if item.get("world_id") is not None else 0,
            "member": 2 if item.get("membership_status") == "member" else 0,
        }

    ranked = sorted(
        (
            (key, sum(axes.values()))
            for key, axes in matrix.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )
    winner_key = ranked[0][0] if ranked else None
    winner_score = ranked[0][1] if ranked else 0
    runner_up = ranked[1][1] if len(ranked) > 1 else 0

    if require_concrete_world:
        proposed = str(reality.selected_world_id) if reality and reality.selected_world_id else (winner_key or "unassigned")
        status = "verified" if reality and reality.selected_world_id else "fragile"
        winner = str(reality.selected_world_id) if reality and reality.selected_world_id else winner_key
    else:
        proposed = reality.top_context_key if reality and reality.top_context_key else "unresolved"
        status = "verified" if winner_key else "insufficient_evidence"
        winner = winner_key

    return MatrixOutcome(
        proposed_key=str(proposed),
        winner_key=winner,
        winner_score=winner_score,
        runner_up_score=runner_up,
        margin=winner_score - runner_up,
        stability=1.0 if winner_key else 0.0,
        status=status,
        matrix=matrix,
    )


async def _resolve_and_verify_semantic_ownership(db, row):
    _claim_context.set(row)
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

        # Reality membership is deliberately independent of semantic ownership.
        # A source can own a proposition while that proposition remains merely a
        # candidate description of one or more concrete worlds.
        reality = await resolve_claim_reality(db, row)
        if reality is not None:
            reality_dependencies = [
                ("decision", f"semantic_owner:{claim_id}"),
                ("claim_context", str(claim_id)),
                ("proposition", str(reality.proposition_id)),
            ]
            if row.get("source_id"):
                reality_dependencies.append(("source", str(row["source_id"])))
            if row.get("world_id"):
                reality_dependencies.append(("world", str(row["world_id"])))
            if row.get("target_world_id"):
                reality_dependencies.append(("world", str(row["target_world_id"])))

            reality_outcome = _matrix_outcome_from_reality(reality)
            await record_validation_decision(
                db,
                decision_type="reality_membership",
                decision_key=str(claim_id),
                subject_type="proposition",
                subject_key=str(reality.proposition_id),
                outcome=reality_outcome,
                selected_value=reality.top_context_key,
                resolver_version=REALITY_RESOLVER_VERSION,
                dependencies=reality_dependencies,
                meta={
                    "memberships": [
                        {
                            **item,
                            "reality_context_id": str(item["reality_context_id"]),
                            "world_id": str(item["world_id"]) if item.get("world_id") else None,
                        }
                        for item in reality.memberships
                    ]
                },
            )

            world_outcome = _matrix_outcome_from_reality(
                reality,
                require_concrete_world=True,
            )
            await record_validation_decision(
                db,
                decision_type="world_assignment",
                decision_key=str(claim_id),
                subject_type="claim",
                subject_key=str(claim_id),
                outcome=world_outcome,
                selected_value=(
                    str(reality.selected_world_id)
                    if reality.world_assignment_verified
                    else None
                ),
                resolver_version="world-assignment-reality-v1",
                dependencies=[
                    ("decision", f"reality_membership:{claim_id}"),
                    ("claim_context", str(claim_id)),
                ] + (
                    [("world", str(reality.selected_world_id))]
                    if reality.selected_world_id
                    else []
                ),
                meta={
                    "authority_boundary": "reality_context_to_aios.world",
                    "world_assignment_verified": reality.world_assignment_verified,
                    "top_context_key": reality.top_context_key,
                },
            )
    return result


async def _validated_character_mention(db, mention):
    return await resolve_character_referent(db, mention, row=_claim_context.get())


# Claim topology remains a materialized view of semantic decisions. The
# ownership/referent decisions are independently revisable and can mark
# topology stale when their evidence changes.
_topology_claims.resolve_semantic_ownership = _resolve_and_verify_semantic_ownership
_topology_claims.resolve_character_mention = _validated_character_mention
_topology.choose_observation_scope = _topology_claims.choose_observation_scope
_topology.derive_claim_topology = _topology_claims.derive_claim_topology

# Keep PostgreSQL topology mutation authoritative while coalescing the expensive
# whole-scope Fuseki rewrite behind a dirty/version projection boundary.
install_deferred_projection(_topology, _topology_claims)

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
    "resolve_claim_reality",
]
