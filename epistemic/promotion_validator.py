from __future__ import annotations

from typing import Any

from aios_app.db import Database
from aios_app.epistemic.hypothesis_validation import evaluate_matrix, record_validation_decision

PROMOTION_VERIFIER_VERSION = "epistemic-promotion-matrix-v1"


async def validate_generated_promotion(
    db: Database,
    *,
    assertion_id: Any,
    world_id: Any,
    proposition_id: Any,
    support_confidence: float,
    conflict_strength: float = 0.0,
    conflict_observed_confidence: float = 0.0,
    competing_proposition_id: Any = None,
) -> tuple[str, dict[str, Any]]:
    support = max(0.0, min(1.0, float(support_confidence)))
    conflict = max(0.0, min(1.0, float(conflict_strength) * float(conflict_observed_confidence)))

    matrix = {
        "CORROBORATE": {
            "direct_support": 3 if support >= 0.75 else (2 if support >= 0.50 else (1 if support > 0 else -2)),
            "conflict": -3 if conflict >= 0.60 else (-1 if conflict > 0 else 1),
            "world": 1,
        },
        "SUPERSEDE": {
            "direct_support": -2 if support >= 0.75 else 0,
            "conflict": 3 if conflict >= 0.60 else (2 if conflict >= 0.35 else (-2 if conflict == 0 else 1)),
            "world": 1,
        },
        "RETAIN_PROVISIONAL": {
            "direct_support": 2 if support == 0 else (1 if support < 0.50 else -1),
            "conflict": 2 if conflict == 0 else (1 if conflict < 0.35 else -2),
            "uncertainty": 2 if max(support, conflict) < 0.50 else 0,
        },
    }

    if conflict >= 0.35:
        proposed = "SUPERSEDE"
    elif support > 0:
        proposed = "CORROBORATE"
    else:
        proposed = "RETAIN_PROVISIONAL"

    outcome = evaluate_matrix(
        matrix,
        proposed_key=proposed,
        min_score=3,
        min_margin=2,
        min_stability=2 / 3,
    )
    action = outcome.winner_key if outcome.status == "verified" and outcome.winner_key else "RETAIN_PROVISIONAL"

    dependencies = [
        ("world_proposition", f"{world_id}:{proposition_id}"),
        ("observation_support", f"{world_id}:{proposition_id}"),
        ("proposition_conflict", str(proposition_id)),
    ]
    if competing_proposition_id:
        dependencies.append(("world_proposition", f"{world_id}:{competing_proposition_id}"))

    await record_validation_decision(
        db,
        decision_type="epistemic_promotion",
        decision_key=str(assertion_id),
        subject_type="world_assertion",
        subject_key=str(assertion_id),
        outcome=outcome,
        selected_value=action,
        resolver_version=PROMOTION_VERIFIER_VERSION,
        dependencies=dependencies,
        meta={
            "support_confidence": support,
            "conflict_score": conflict,
            "competing_proposition_id": str(competing_proposition_id) if competing_proposition_id else None,
        },
    )
    return action, outcome.as_meta()
