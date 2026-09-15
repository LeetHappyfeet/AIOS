from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


FACET_RENDER_VERSION = "semantic-facet-render-v1"


@dataclass(frozen=True)
class FacetMember:
    text: str
    confidence: float
    stance: str
    polarity: int
    atom_id: str | None = None
    proposition_id: str | None = None


def collapse_facet_members(
    members: Iterable[dict[str, Any]],
    *,
    facet: str,
    facet_slot: str,
    exclusive: bool,
) -> dict[str, Any]:
    """Build a compact retrieval unit without erasing the underlying atoms.

    Multi-valued descriptive facets retain every compatible member. Exclusive
    facets surface competing values as contested rather than pretending that
    all members can be simultaneously true.
    """
    normalized: list[FacetMember] = []
    for member in members:
        text = str(member.get("text") or "").strip()
        if not text:
            continue
        normalized.append(
            FacetMember(
                text=text,
                confidence=float(member.get("confidence") or 0.0),
                stance=str(member.get("stance") or "unresolved"),
                polarity=int(member.get("polarity") or 1),
                atom_id=str(member.get("atom_id")) if member.get("atom_id") else None,
                proposition_id=(
                    str(member.get("proposition_id")) if member.get("proposition_id") else None
                ),
            )
        )

    normalized.sort(key=lambda item: (-item.confidence, item.text))
    contested = any(item.stance == "unresolved" for item in normalized)
    if exclusive:
        positive_values = {item.text for item in normalized if item.polarity == 1}
        negative_values = {item.text for item in normalized if item.polarity == -1}
        contested = contested or len(positive_values) > 1 or bool(positive_values & negative_values)

    confidence = max((item.confidence for item in normalized), default=0.0)
    return {
        "facet": facet,
        "facet_slot": facet_slot,
        "facet_exclusive": exclusive,
        "contested": contested,
        "confidence": confidence,
        "member_count": len(normalized),
        "members": [item.__dict__ for item in normalized],
        "renderer_version": FACET_RENDER_VERSION,
    }
