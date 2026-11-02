from __future__ import annotations

"""Public proposition normalizer facade for semantic-engine v4."""

import json
import logging
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.semantic_integrity import validate_claim, INTEGRITY_VERSION
from aios_app.epistemic.semantic_repair import repair_claim, REPAIR_VERSION
from aios_app.epistemic import normalizer_legacy as legacy
from aios_app.epistemic.context_resolver import RESOLVER_VERSION
from aios_app.epistemic import message_cognition as _message_cognition
from aios_app.epistemic.epistemic_scope import install_message_cognition_scope_guard

NORMALIZER_VERSION = "proposition-v3-semantic"
logger = logging.getLogger(__name__)

# Install the same conservative assertion boundary used by structural
# normalization on the generation-critical cognition path. This facade is
# imported during epistemic package bootstrap, before HUD readiness imports the
# cognition functions.
install_message_cognition_scope_guard(_message_cognition)

# be_definition_of was historically treated as a single-valued semantic slot.
# That made unrelated descriptions conflict (digital vs direct vs character).
# v4 semantic interpretation narrows identity before normalization, and strict
# identity conflicts belong to semantic atoms/reconciliation rather than this
# broad syntactic predicate.
legacy.EXCLUSIVE_PREDICATES.discard("be_definition_of")
EXCLUSIVE_PREDICATES = legacy.EXCLUSIVE_PREDICATES

normalize_components = legacy.normalize_components
ensure_proposition = legacy.ensure_proposition


async def _apply_epistemic_admission(db: Database, *, claim_id: UUID) -> None:
    """Keep non-assertive atoms without promoting them as truth-bearing memory."""
    # Modality belongs to this observation/frame occurrence, not proposition
    # identity: the same proposition may be asserted in one source and merely
    # supposed in another. observation_proposition gives us that exact edge.
    await db.execute(
        """
        DELETE FROM aios.proposition_evidence pe
        USING aios.observation o,
              aios.observation_proposition op,
              aios.claim_semantic_frame f
        WHERE pe.observation_id=o.observation_id
          AND op.observation_id=pe.observation_id
          AND op.proposition_id=pe.proposition_id
          AND f.frame_id=op.frame_id
          AND o.claim_id=$1
          AND pe.evidence_role IN ('supposition','conditional','counterfactual','inquiry')
          AND lower(COALESCE(f.modality,'asserted')) IN (
              'hypothetical','conditional','counterfactual','question'
          )
        """,
        claim_id,
    )
    await db.execute(
        """
        UPDATE aios.proposition_evidence pe
        SET evidence_role = CASE lower(COALESCE(f.modality,'asserted'))
            WHEN 'hypothetical' THEN 'supposition'
            WHEN 'conditional' THEN 'conditional'
            WHEN 'counterfactual' THEN 'counterfactual'
            WHEN 'question' THEN 'inquiry'
            ELSE pe.evidence_role
        END,
            meta = COALESCE(pe.meta,'{}'::jsonb) || jsonb_build_object(
                'epistemic_admission_policy', 'epistemic-scope-v1',
                'effective_modality', COALESCE(f.modality,'asserted'),
                'semantic_frame_id', f.frame_id::text
            )
        FROM aios.observation o,
             aios.observation_proposition op,
             aios.claim_semantic_frame f
        WHERE pe.observation_id=o.observation_id
          AND op.observation_id=pe.observation_id
          AND op.proposition_id=pe.proposition_id
          AND f.frame_id=op.frame_id
          AND o.claim_id=$1
          AND lower(COALESCE(f.modality,'asserted')) IN (
              'hypothetical','conditional','counterfactual','question'
          )
        """,
        claim_id,
    )

    # The legacy normalizer creates one primary character-acquisition edge.
    # Remove that edge when the primary frame is non-assertive while retaining
    # the proposition, observation, frames, and scoped evidence.
    await db.execute(
        """
        DELETE FROM aios.knowledge_acquisition_event kae
        USING aios.claim_semantic_frame_projection sfp,
              aios.claim_semantic_frame f
        WHERE kae.claim_id=$1
          AND sfp.claim_id=kae.claim_id
          AND f.frame_id=sfp.primary_frame_id
          AND lower(COALESCE(f.modality,'asserted')) IN (
              'hypothetical','conditional','counterfactual','question'
          )
        """,
        claim_id,
    )


async def _record_normalization(db, claim_id, revision, receipt, outcome):
    # Compare-and-set: a source/context edit or deterministic frame repair
    # during this attempt must not stamp a blocked receipt on the newer input.
    return await db.fetchval("""
        UPDATE aios.claim_normalization_state ns
        SET attempted_revision=$2,outcome=$3,normalizer_version=$4,
            validator_version=$5,repair_version=$6,integrity_status=$7,
            reason_codes=$8::jsonb,integrity_revision_key=si.revision_key,
            attempted_at=now(),updated_at=now()
        FROM aios.claim_semantic_integrity si
        WHERE ns.claim_id=$1 AND ns.input_revision=$2 AND si.claim_id=ns.claim_id
        RETURNING ns.claim_id
    """,claim_id,revision,outcome,NORMALIZER_VERSION,INTEGRITY_VERSION,REPAIR_VERSION,
        receipt.status,json.dumps(receipt.reasons))


async def normalize_claim_once(db: Database, *, claim_id: UUID) -> UUID | None:
    state = await db.fetchrow("SELECT * FROM aios.claim_normalization_state WHERE claim_id=$1", claim_id)
    if state is None:
        raise RuntimeError(f"Missing normalization revision state for {claim_id}; run migrations")
    same_policy = (state["normalizer_version"] == NORMALIZER_VERSION
                   and state["validator_version"] == INTEGRITY_VERSION
                   and state["repair_version"] == REPAIR_VERSION)
    if (state["outcome"] == "blocked" and state["attempted_revision"] == state["input_revision"]
            and same_policy):
        return None  # Also harmless for duplicate jobs already in the queue.

    # Source integrity is an admission condition, not a downstream attention hint.
    receipt = await validate_claim(db, claim_id=claim_id)
    if receipt.status != "valid":
        if state["attempted_revision"] != state["input_revision"] or not same_policy:
            # The old cap belongs to the previous input/policy revision.
            # Audit revisions remain intact; new evidence gets a repair chance.
            await db.execute("""UPDATE aios.claim_semantic_integrity
                SET repair_attempts=0,repair_state='pending' WHERE claim_id=$1""", claim_id)
        receipt = await repair_claim(db, claim_id=claim_id, receipt=receipt)
    if receipt.status != "valid":
        # Preserve the source claim and frames, but revoke existing evidence.
        # The same claim remains repairable without publishing another occurrence.
        await db.execute(
            """DELETE FROM aios.proposition_evidence pe
               USING aios.observation o
               WHERE pe.observation_id=o.observation_id AND o.claim_id=$1""",
            claim_id,
        )
        recorded = await _record_normalization(db,claim_id,state["input_revision"],receipt,"blocked")
        logger.info("Normalization blocked claim=%s revision=%s integrity=%s reasons=%s current=%s",
                    claim_id,state["input_revision"],receipt.status,receipt.reasons,bool(recorded))
        return None
    proposition_id = await legacy.normalize_claim_once(db, claim_id=claim_id)
    await _apply_epistemic_admission(db, claim_id=claim_id)

    # Acquisition revalidation/retraction is intentionally owned by the
    # project_character_knowledge stage. Keeping normalization confined to
    # normalization-owned tables avoids cross-stage row/FK lock cycles, except
    # for the narrow non-assertive admission edge removed above before it can
    # become established character memory.

    # Keep observation provenance honest about the resolver that actually
    # classified the claim. Older code stamped v3 as a literal constant.
    context = await db.fetchrow(
        "SELECT resolver_version, meta FROM aios.claim_context_resolution WHERE claim_id=$1",
        claim_id,
    )
    resolver_version = context["resolver_version"] if context else RESOLVER_VERSION
    await db.execute(
        """
        UPDATE aios.observation
        SET meta=COALESCE(meta,'{}'::jsonb) || $2::jsonb
        WHERE claim_id=$1
        """,
        claim_id,
        json.dumps({
            "normalizer_version": NORMALIZER_VERSION,
            "context_resolver_version": resolver_version,
            "epistemic_admission_policy": "epistemic-scope-v1",
        }),
    )
    await _record_normalization(db,claim_id,state["input_revision"],receipt,"normalized")
    return proposition_id


async def _detect_conflicts(db: Database, *, proposition_id: UUID) -> int:
    return await legacy._detect_conflicts(db, proposition_id=proposition_id)
