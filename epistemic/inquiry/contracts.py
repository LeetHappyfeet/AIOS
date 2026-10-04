"""Shared contract for source repair and downstream character inquiry."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

_SCOPES = {"source_local", "character_accessible"}
_KINDS = {"unresolved_reference", "attribution_unresolved", "episodic_gap",
          "world_gap", "concept_gap", "failed_retrieval", "ambiguous_meaning",
          "conflicting_evidence", "explicit_question"}


@dataclass(frozen=True)
class InquiryDemand:
    instance_id: UUID
    origin: str
    uncertainty_kind: str
    question: str
    evidence_revision: str
    policy_version: str = "character-inquiry-v1"
    source_node_id: UUID | None = None
    source_span: tuple[int, int] | None = None
    anchor_text: str = ""
    evidence_scope: str = "character_accessible"
    priority: int = 100

    def __post_init__(self) -> None:
        if self.evidence_scope not in _SCOPES:
            raise ValueError("unsupported inquiry evidence scope")
        if self.uncertainty_kind not in _KINDS:
            raise ValueError("unsupported inquiry uncertainty kind")
        if not self.question.strip() or len(self.question) > 600:
            raise ValueError("inquiry requires a bounded question")
        if not self.evidence_revision.strip():
            raise ValueError("inquiry needs a source/evidence revision")
        if self.evidence_scope == "source_local" and not self.source_node_id:
            raise ValueError("source-local inquiry requires an anchored source node")
        if self.source_span is not None and (self.source_span[0] < 0 or
                                            self.source_span[1] <= self.source_span[0]):
            raise ValueError("invalid source span")

    @property
    def fingerprint(self) -> str:
        data = (str(self.instance_id), str(self.source_node_id or ""),
                self.source_span, self.anchor_text, self.origin, self.uncertainty_kind,
                " ".join(self.question.casefold().split()), self.evidence_scope,
                self.evidence_revision, self.policy_version)
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {"instance_id": str(self.instance_id), "source_node_id": str(self.source_node_id)
                if self.source_node_id else None, "source_span": self.source_span,
                "anchor_text": self.anchor_text, "origin": self.origin, "uncertainty_kind": self.uncertainty_kind,
                "question": self.question, "evidence_scope": self.evidence_scope,
                "evidence_revision": self.evidence_revision, "policy_version": self.policy_version,
                "priority": self.priority}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InquiryDemand":
        span = value.get("source_span")
        return cls(instance_id=UUID(str(value["instance_id"])),
                   source_node_id=UUID(str(value["source_node_id"]))
                   if value.get("source_node_id") else None,
                   source_span=tuple(span) if span is not None else None,
                   anchor_text=str(value.get("anchor_text") or ""),
                   origin=str(value["origin"]), uncertainty_kind=str(value["uncertainty_kind"]),
                   question=str(value["question"]), evidence_scope=str(
                       value.get("evidence_scope") or "character_accessible"),
                   evidence_revision=str(value["evidence_revision"]),
                   policy_version=str(value.get("policy_version") or "character-inquiry-v1"),
                   priority=int(value.get("priority") or 100))


@dataclass(frozen=True)
class InquiryHit:
    source: str
    evidence_id: str
    text: str
    provenance: dict[str, Any] = field(default_factory=dict)
    durable_knowledge: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "evidence_id": self.evidence_id,
                "text": self.text, "provenance": self.provenance,
                "durable_knowledge": self.durable_knowledge}


@dataclass(frozen=True)
class InquiryEvidence:
    status: str
    hits: tuple[InquiryHit, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"resolved", "partial", "unresolved", "conflicting"}:
            raise ValueError("invalid inquiry evidence status")

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason,
                "hits": [hit.as_dict() for hit in self.hits]}
