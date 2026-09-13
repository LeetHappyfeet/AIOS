from __future__ import annotations

from typing import Dict

from .domain import CausalDomain
from .domains import LocationDomain, ScalarStateDomain


class CausalDomainRegistry:
    """Registry of deterministic policies; the HUD/plugin layer is not consulted."""

    def __init__(self) -> None:
        self._domains: Dict[str, CausalDomain] = {}
        self.register(LocationDomain())
        self.register(ScalarStateDomain())

    def register(self, domain: CausalDomain) -> None:
        key = str(domain.domain_id).strip()
        if not key:
            raise ValueError("causal domain_id cannot be empty")
        self._domains[key] = domain

    def get(self, domain_id: str) -> CausalDomain:
        try:
            return self._domains[domain_id]
        except KeyError as exc:
            raise ValueError(f"unknown causal domain '{domain_id}'") from exc

    def contains(self, domain_id: str) -> bool:
        return domain_id in self._domains
