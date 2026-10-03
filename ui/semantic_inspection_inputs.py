"""Pure input/output helpers for the operator neighborhood inspector."""
from __future__ import annotations

from typing import Any
from uuid import UUID


def build_inspection_request(collection: str, cluster: str, points: str,
                             neighbors: int, offset: int, evidence_offset: int) -> dict[str, Any]:
    if not 0 <= neighbors <= 511 or offset < 0 or evidence_offset < 0:
        raise ValueError("Use 0–511 neighbors and nonnegative offsets.")
    ids = list(dict.fromkeys((points or "").replace(",", " ").split()))
    cluster = (cluster or "").strip()
    payload = {"collection": (collection or "").strip() or None,
               "neighbors": neighbors, "offset": offset, "evidence_offset": evidence_offset}
    if neighbors:
        if not ids and cluster:
            payload.update(cluster_id=str(UUID(cluster)), neighbors=0)
            return payload
        if len(ids) != 1:
            raise ValueError("To expand neighbors, click one member row or paste exactly one Qdrant point ID. "
                             "The neighbor count controls how many points to find; it does not select a seed.")
        if offset:
            raise ValueError("Set member offset to 0 when expanding around one seed.")
        payload["point_ids"] = [str(UUID(ids[0]))]
    elif cluster:
        payload["cluster_id"] = str(UUID(cluster))
    elif ids:
        payload["point_ids"] = [str(UUID(value)) for value in ids]
        if len(ids) > 512:
            raise ValueError("Inspect at most 512 point IDs at once.")
    else:
        raise ValueError("Enter a cluster ID or at least one Qdrant point ID.")
    return payload


def api_error_detail(body: Any, fallback: str) -> str:
    if not isinstance(body, dict):
        return fallback
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        messages = []
        for error in detail:
            if isinstance(error, dict):
                location = ".".join(str(value) for value in error.get("loc", []) if value != "body")
                messages.append(f"{location}: {error.get('msg', 'Invalid value')}" if location else str(error.get("msg", "Invalid value")))
        if messages:
            return "; ".join(messages)
    return fallback


def member_seed(result: dict[str, Any] | None, row_index: int) -> tuple[str, str, str, int]:
    members = (result or {}).get("members", [])
    if not 0 <= row_index < len(members):
        raise ValueError("Inspect a group first, then click a member row to select its seed.")
    points = members[row_index].get("points", [])
    if not points:
        raise ValueError("This member has no Qdrant point. Choose a member whose vector is available.")
    # Ownership collections use distinct point UUIDs, not proposition UUIDs.
    point_id = str(UUID(points[0]["point_id"]))
    return point_id, "", result["collection"], 0
