from __future__ import annotations

from typing import Any, Optional

from aios_app.db import Database
from aios_app.epistemic.hypothesis_validation import evaluate_matrix, record_validation_decision

ENTITY_VERIFIER_VERSION = "entity-referent-matrix-v1"


def _method_score(method: str) -> int:
    return {
        "character_id_exact": 3,
        "display_name_exact": 3,
        "canonical_name_exact": 3,
        "alias_exact": 3,
    }.get(method, 0)


async def resolve_character_referent(
    db: Database,
    mention: str,
    *,
    row: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    value = str(mention or "").strip()
    if not value:
        return None

    context = row or {}
    rows = await db.fetch(
        """
        SELECT DISTINCT
            ci.character_id,
            COALESCE(NULLIF(ci.display_name,''), NULLIF(ci.canonical_name,''), ci.character_id) AS label,
            CASE
                WHEN lower(trim(ci.character_id))=lower(trim($1)) THEN 'character_id_exact'
                WHEN lower(trim(COALESCE(ci.display_name,'')))=lower(trim($1)) THEN 'display_name_exact'
                WHEN lower(trim(COALESCE(ci.canonical_name,'')))=lower(trim($1)) THEN 'canonical_name_exact'
                ELSE 'alias_exact'
            END AS method,
            EXISTS (
                SELECT 1 FROM aios.character_instance inst
                WHERE inst.character_id=ci.character_id
                  AND ($2::uuid IS NULL OR inst.current_world_id=$2)
            ) AS world_present
        FROM aios.character_identity ci
        LEFT JOIN aios.character_alias ca ON ca.character_id=ci.character_id
        WHERE lower(trim(ci.character_id))=lower(trim($1))
           OR lower(trim(COALESCE(ci.display_name,'')))=lower(trim($1))
           OR lower(trim(COALESCE(ci.canonical_name,'')))=lower(trim($1))
           OR lower(trim(COALESCE(ca.alias,'')))=lower(trim($1))
        ORDER BY ci.character_id
        """,
        value,
        context.get("world_id"),
    )
    if not rows:
        return None

    expected = str(context.get("origin_character_id") or context.get("speaker_id") or "").strip().lower()
    matrix: dict[str, dict[str, int]] = {}
    candidates: dict[str, dict[str, Any]] = {}
    for candidate in rows:
        cid = str(candidate["character_id"])
        key = f"character:{cid}"
        method = str(candidate["method"])
        candidates[key] = dict(candidate)
        matrix[key] = {
            "identity": _method_score(method),
            "role": 3 if expected and cid.lower() == expected else (0 if not expected else -2),
            "world": 1 if candidate["world_present"] else 0,
        }

    proposed = sorted(matrix, key=lambda key: (-sum(matrix[key].values()), key))[0]
    outcome = evaluate_matrix(
        matrix,
        proposed_key=proposed,
        min_score=3,
        min_margin=2,
        min_stability=2 / 3,
    )
    selected = outcome.winner_key if outcome.status == "verified" else None

    claim_id = context.get("claim_id")
    subject_key = str(claim_id) if claim_id else value.lower()
    decision_key = f"{claim_id}:{value.lower()}" if claim_id else f"mention:{value.lower()}"
    dependencies = [("character_identity", value.lower())]
    if claim_id:
        dependencies.append(("decision", f"semantic_owner:{claim_id}"))
        dependencies.append(("claim_context", str(claim_id)))
    if context.get("world_id"):
        dependencies.append(("world", str(context["world_id"])))
    if context.get("speaker_id"):
        dependencies.append(("speaker_identity", str(context["speaker_id"]).strip().lower()))

    await record_validation_decision(
        db,
        decision_type="entity_referent",
        decision_key=decision_key,
        subject_type="claim" if claim_id else "surface_mention",
        subject_key=subject_key,
        outcome=outcome,
        selected_value=selected,
        resolver_version=ENTITY_VERIFIER_VERSION,
        dependencies=dependencies,
        meta={"surface_mention": value},
    )

    if not selected:
        return None
    winner = candidates[selected]
    return {
        "character_id": str(winner["character_id"]),
        "label": str(winner["label"] or winner["character_id"]),
        "method": str(winner["method"]),
        "verification": outcome.as_meta(),
    }
