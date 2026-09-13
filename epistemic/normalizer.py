from __future__ import annotations

"""Public proposition normalizer facade for semantic-engine v4."""

import json
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic import normalizer_legacy as legacy
from aios_app.epistemic.context_resolver import RESOLVER_VERSION
from aios_app.epistemic.knowledge import reconcile_context_acquisitions_for_claim

NORMALIZER_VERSION = "proposition-v3-semantic"

# be_definition_of was historically treated as a single-valued semantic slot.
# That made unrelated descriptions conflict (digital vs direct vs character).
# v4 semantic interpretation narrows identity before normalization, and strict
# identity conflicts belong to semantic atoms/reconciliation rather than this
# broad syntactic predicate.
legacy.EXCLUSIVE_PREDICATES.discard("be_definition_of")
EXCLUSIVE_PREDICATES = legacy.EXCLUSIVE_PREDICATES

normalize_components = legacy.normalize_components
ensure_proposition = legacy.ensure_proposition


async def normalize_claim_once(db: Database, *, claim_id: UUID) -> UUID:
    proposition_id = await legacy.normalize_claim_once(db, claim_id=claim_id)

    # The compatibility normalizer may have emitted a context-generated
    # acquisition from an older/coarser interpretation. Revalidate it against
    # the current semantic context before it can reach /char knowledge.
    await reconcile_context_acquisitions_for_claim(db, claim_id=claim_id)

    # Keep observation provenance honest about the resolver that actually
    # classified the claim.  Older code stamped v3 as a literal constant.
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
        }),
    )
    return proposition_id


async def _detect_conflicts(db: Database, *, proposition_id: UUID) -> int:
    return await legacy._detect_conflicts(db, proposition_id=proposition_id)
