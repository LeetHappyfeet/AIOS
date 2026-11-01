"""Lossless prompt references. These encode identity, never authorization.

No truncation, mutable alias table or model-selected mapping is involved.
Callers must still run their ordinary ownership/branch/lifecycle checks.
"""
import base64
import re
from uuid import UUID


def short_reference(value, kind: str) -> str:
    try:
        identifier = value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return str(value)
    return kind + "_" + base64.urlsafe_b64encode(identifier.bytes).decode().rstrip("=")


def expand_reference(value, kind: str):
    if not isinstance(value, str) or not re.match(r"^[gqri]_", value):
        return value
    if not re.fullmatch(re.escape(kind) + r"_[A-Za-z0-9_-]{22}", value):
        raise ValueError("Invalid reference type or encoding")
    identifier = UUID(bytes=base64.b64decode(value[2:] + "==", altchars=b"-_", validate=True))
    if short_reference(identifier, kind) != value:
        raise ValueError("Noncanonical reference encoding")
    return str(identifier)
