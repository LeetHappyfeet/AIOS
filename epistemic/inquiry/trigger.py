"""Deterministic triggers. Do not implement a second linguistic parser here."""
from __future__ import annotations
import hashlib
from uuid import UUID
from .contracts import InquiryDemand

_V11_REASONS = {
    "source_admission:unresolved_reference:objective_contains_unresolved_reference":
        "unresolved_reference",
    "source_admission:attribution_unresolved:missing_source_text":
        "attribution_unresolved",
}


def demand_from_v11_rejection(
    *, instance_id: UUID, source_node_id: UUID, source_text: str,
    rejection_reason: str, source_index: int, admission_version: str,
) -> InquiryDemand | None:
    """Only actionable, source-bound V11 diagnostics are eligible.
    
    No automatic inquiry for scene-only, figurative, or insufficient commitments.
    A rejected candidate never becomes an admitted cognitive/goal unit here.
    """
    kind = _V11_REASONS.get(rejection_reason)
    if kind is None or not str(source_text or "").strip():
        return None
    source = str(source_text).strip()
    revision = hashlib.sha256((admission_version + "\0" + str(source_index)
                               + "\0" + source).encode()).hexdigest()
    question = ("Find the antecedent of the unresolved reference in this source: "
                if kind == "unresolved_reference" else
                "Check original speaker and ownership for this source: ") + source[:420]
    return InquiryDemand(
        instance_id=instance_id, source_node_id=source_node_id, origin="message_cognition",
        uncertainty_kind=kind, question=question[:600], evidence_scope="source_local",
        evidence_revision=revision, policy_version=admission_version or "goal-source-admission-v1",
    )


def should_escalate(demand: InquiryDemand, result_status: str, *,
                    existing_model_calls: int = 0) -> bool:
    """One optional planning call, never for already supported evidence."""
    return existing_model_calls == 0 and result_status in {"partial", "unresolved"} and (
        demand.uncertainty_kind in {"unresolved_reference", "attribution_unresolved",
          "ambiguous_meaning", "failed_retrieval", "explicit_question", "concept_gap"}
    )
