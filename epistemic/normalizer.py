from __future__ import annotations

"""Public proposition normalizer facade for semantic-engine v4."""

import json
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic import normalizer_legacy as legacy
from aios_app.epistemic.context_resolver import RESOLVER_VERSION
from aios_app.epistemic import message_cognition as _message_cognition
from aios_app.epistemic.epistemic_scope import install_message_cognition_scope_guard

NORMALIZER_VERSION = "proposition-v3-semantic"

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


async def normalize_claim_once(db: Database, *, claim_id: UUID) -> UUID:
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
    return proposition_id


async def _detect_conflicts(db: Database, *, proposition_id: UUID) -> int:
    return await legacy._detect_conflicts(db, proposition_id=proposition_id)
