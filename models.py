from datetime import datetime

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Literal
from uuid import UUID


# -------------------------------------------------
# Enums / Literals
# -------------------------------------------------

ActorType = Literal["user", "character", "agent", "system", "tool", "source"]

EventKind = Literal[
    "chat_message",
    "heartbeat",
    "status",
    "tool_call",
    "tool_result",
    "memory_inject",
    "system",
    "other",
    "document",
    "paragraph",
    "observation",
]


# -------------------------------------------------
# Session models
# -------------------------------------------------

class SessionCreate(BaseModel):
    topic: str = ""
    source: Optional[str] = None
    source_session_id: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class SessionOut(BaseModel):
    session_id: UUID
    topic: str


# -------------------------------------------------
# Ingest models
# -------------------------------------------------

class IngestIn(BaseModel):
    session_id: UUID

    speaker_id: Optional[str] = None
    speaker_type: ActorType = "user"
    recipient_id: Optional[str] = None

    # Explicit first-person identity for roleplay/control layers. When set,
    # semantic pivot resolution treats "I" as this identity while preserving
    # speaker_id as the transport/source actor.
    viewpoint_id: Optional[str] = None

    character_id: str
    user_name: str

    text: str
    kind: EventKind = "chat_message"

    # raw "extra" fields; lands in payload jsonb
    payload: Dict[str, Any] = Field(default_factory=dict)

    # optional for stable dedupe
    dedupe_key: Optional[str] = None

    # optional: group chat, world instance, etc.
    scope_key: Optional[str] = None


class ExternalObservationIn(BaseModel):
    """
    Generic provenance-safe ingress for non-character observations.

    target_character_id and target_world_id are downstream routing/enrichment
    hints only. They never become origin character ownership or world truth.
    """
    source_id: str = Field(min_length=1)
    source_kind: str = Field(default="external", min_length=1)
    source_name: Optional[str] = None
    source_uri: Optional[str] = None
    source_event_id: Optional[str] = None
    source_meta: Dict[str, Any] = Field(default_factory=dict)

    session_id: Optional[UUID] = None
    event_time: Optional[datetime] = None

    speaker_id: Optional[str] = None
    speaker_type: ActorType = "source"
    recipient_id: Optional[str] = None
    viewpoint_id: Optional[str] = None

    target_character_id: Optional[str] = None
    target_world_id: Optional[UUID] = None

    text: str
    kind: Literal["observation"] = "observation"
    payload: Dict[str, Any] = Field(default_factory=dict)
    dedupe_key: Optional[str] = None
    scope_key: Optional[str] = None


class IngestOut(BaseModel):
    ok: bool
    event_id: int
    node_id: UUID
    timeline_id: UUID


# -------------------------------------------------
# Memory models
# -------------------------------------------------

class MemoryMatch(BaseModel):
    content: str
    score: float = 0.0
    meta: Dict[str, Any] = Field(default_factory=dict)


class MemoryOut(BaseModel):
    timeline_id: Optional[UUID] = None
    vector_matches: List[MemoryMatch] = Field(default_factory=list)


# -------------------------------------------------
# Shared world runtime models
# -------------------------------------------------

ControllerType = Literal["human", "agent", "system", "tool", "script"]


class CharacterActivateIn(BaseModel):
    user_name: str
    session_id: Optional[UUID] = None
    scope_key: str = "default"
    world_id: Optional[UUID] = None
    world_key: Optional[str] = None
    controller_type: ControllerType = "agent"
    controller_ref: Optional[str] = None


class CharacterActivateOut(BaseModel):
    character_id: str
    instance_id: UUID
    entity_id: UUID
    world_id: UUID
    timeline_id: UUID
    head_node_id: Optional[UUID] = None
    state_version: int
    lifecycle_state: str


class WorldEntityCreateIn(BaseModel):
    entity_key: Optional[str] = None
    entity_type: str = "object"
    display_name: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class WorldRelationCreateIn(BaseModel):
    subject_entity_id: UUID
    relation_type: str
    object_entity_id: UUID
    meta: Dict[str, Any] = Field(default_factory=dict)


class WorldRulePutIn(BaseModel):
    rule_type: str = "constraint"
    enabled: bool = True
    priority: int = 100
    rule_data: Dict[str, Any] = Field(default_factory=dict)


class WorldActionIn(BaseModel):
    expected_state_version: int
    action_type: str
    target_entity_id: Optional[UUID] = None
    text: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class CharacterForkIn(BaseModel):
    target_world_id: Optional[UUID] = None
    target_world_key: Optional[str] = None


class EntityControllerIn(BaseModel):
    controller_type: ControllerType
    controller_ref: str
    authority: str = "primary"


class KnowledgeAcquireIn(BaseModel):
    proposition_id: Optional[UUID] = None
    claim_id: Optional[UUID] = None
    acquisition_mode: str
    epistemic_status: str = "observed"
    confidence: Optional[float] = None
    source_entity_id: Optional[UUID] = None
    dag_node_id: Optional[UUID] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class GeneratedFactIn(BaseModel):
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[str] = None
    raw_text: str
    confidence: float = 0.35
    generated_at_node_id: Optional[UUID] = None
    reason: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class WorldObservedFactIn(BaseModel):
    proposition_id: Optional[UUID] = None
    claim_id: Optional[UUID] = None
    confidence: float = 0.7
    reason: str = "explicit world bootstrap/import"


class LongDocumentIn(BaseModel):
    text: str
    source_type: str = "document"
    source_uri: Optional[str] = None
    title: Optional[str] = None
    source_name: str = "long_document"


class CharacterEpistemicProfileIn(BaseModel):
    skepticism: float = 0.5
    curiosity: float = 0.5
    authority_trust: float = 0.5
    novelty_seeking: float = 0.5
    emotional_reactivity: float = 0.5
    retention: float = 0.7
    source_trust: Dict[str, float] = Field(default_factory=dict)
    topic_interest: Dict[str, float] = Field(default_factory=dict)
    domain_expertise: Dict[str, float] = Field(default_factory=dict)
    trait_weights: Dict[str, float] = Field(default_factory=dict)


class DocumentAcquireIn(BaseModel):
    acquisition_mode: str = "read_document"
    epistemic_status: str = "observed"
    confidence: Optional[float] = None


class EpistemicSearchIn(BaseModel):
    query: str
    limit: int = 25
    character_id: Optional[str] = None
    instance_id: Optional[UUID] = None
    source_key: Optional[str] = None
    include_conflicts: bool = True
