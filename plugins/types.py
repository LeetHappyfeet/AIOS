from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID


@dataclass(frozen=True)
class PluginRuntimeContext:
    instance_id: UUID
    character_id: str
    entity_id: UUID
    world_id: UUID
    world_key: str
    timeline_id: UUID
    location_entity_id: Optional[UUID] = None
    raw_state: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalSignal:
    key: str
    value: Any
    role: str = "context"
    strength: float = 1.0
    entity_id: Optional[UUID] = None

    def as_focus_text(self) -> str:
        return f"{self.key} {self.value}"


@dataclass(frozen=True)
class HUDField:
    key: str
    value: Any
    label: Optional[str] = None
    field_type: str = "text"
    unit: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    retrieval_role: str = "none"
    retrieval_strength: float = 1.0
    meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HUDSection:
    key: str
    title: str
    fields: tuple[HUDField, ...] = ()
    priority: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "priority": self.priority,
            "fields": [field.to_dict() for field in self.fields],
        }


@dataclass(frozen=True)
class PluginAction:
    key: str
    label: Optional[str] = None
    description: Optional[str] = None
    requires_confirmation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PluginContribution:
    plugin_id: str
    sections: tuple[HUDSection, ...] = ()
    retrieval_signals: tuple[RetrievalSignal, ...] = ()
    actions: tuple[PluginAction, ...] = ()
    state: Mapping[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_seconds: Optional[float] = None
    source: Optional[str] = None

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        if self.ttl_seconds is None:
            return False
        now = now or datetime.now(timezone.utc)
        observed = self.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return (now - observed).total_seconds() > self.ttl_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "sections": [section.to_dict() for section in self.sections],
            "actions": [action.to_dict() for action in self.actions],
            "state": dict(self.state),
            "observed_at": self.observed_at.isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "source": self.source,
        }
