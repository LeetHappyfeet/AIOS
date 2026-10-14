"""Deterministic, offset-preserving passage selection for source inquiry.

Selection is a retrieval concern, never a semantic or goal-admission verdict.
"""
from __future__ import annotations
from dataclasses import dataclass
import re

SELECTOR_VERSION = "inquiry-passage-v2"


@dataclass(frozen=True)
class PassageSelection:
    text: str
    start_char: int
    end_char: int
    anchor_start_char: int | None
    anchor_end_char: int | None
    method: str
    anchor_verified: bool
    ambiguous: bool = False


def locate_anchor(source: str, anchor_text: str, span: tuple[int, int] | None = None
                  ) -> tuple[int, int, str] | None:
    """Coordinates are original DAG-message offsets, never sentence-relative."""
    if span:
        lo, hi = span
        if 0 <= lo < hi <= len(source):
            expected = anchor_text.strip()
            if not expected or source[lo:hi].strip() == expected:
                return lo, hi, "verified_span"
    needle = anchor_text.strip()
    if not needle:
        return None
    matches = [m.span() for m in re.finditer(re.escape(needle), source, flags=re.I)]
    if len(matches) == 1:
        return *matches[0], "unique_exact_text"
    return None


def select_passage(source: str, *, anchor_text: str = "",
                   source_span: tuple[int, int] | None = None,
                   budget: int = 650, uncertainty_kind: str = "unresolved_reference"
                   ) -> PassageSelection:
    """Preserve the target plus preceding context; never default to a source tail.

    An unlocatable/repeated anchor produces an explicitly unverified selection.
    Its excerpt must not be represented as a supported referent.
    """
    source = str(source or "")
    budget = max(100, min(int(budget), 650))
    located = locate_anchor(source, anchor_text, source_span)
    if located is None:
        # Absence is preferable to an arbitrarily plausible unrelated excerpt.
        if anchor_text.strip():
            repeated = len(re.findall(re.escape(anchor_text.strip()), source, re.I)) > 1
            return PassageSelection("", 0, 0, None, None,
                                    "ambiguous_anchor" if repeated else "anchor_not_found",
                                    False, repeated)
        return PassageSelection(source[:budget], 0, min(len(source), budget),
                                None, None, "unanchored_prefix", False)
    lo, hi, method = located
    if hi - lo >= budget:
        start, end = lo, min(len(source), lo + budget)
    else:
        before = int((budget - (hi - lo)) * (0.78 if uncertainty_kind ==
                     "unresolved_reference" else 0.58))
        start = max(0, lo - before)
        end = min(len(source), start + budget)
        # Preserve the full anchor even if the preceding window hit source start.
        if end < hi:
            start = max(0, hi - budget)
            end = min(len(source), start + budget)
        # Expand to nearby whitespace without losing anchor or exceeding budget.
        if start and start < lo:
            boundary = source.find(" ", start, min(lo, start + 24))
            if boundary >= 0:
                start = boundary + 1
        if end < len(source) and end > hi:
            boundary = source.rfind(" ", max(hi, end - 24), end)
            if boundary >= hi:
                end = boundary
    return PassageSelection(source[start:end], start, end, lo, hi,
                            method, True)
