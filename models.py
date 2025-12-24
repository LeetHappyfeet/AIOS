from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Literal
from uuid import UUID


ActorType = Literal["user", "character", "agent", "system", "tool"]
EventKind = Literal[
    "chat_message",
    "heartbeat",
    "status",
    "tool_call",
    "tool_result",
    "memory_inject",
    "system",
    "other",
]


class SessionCreate(BaseModel):
    topic: str = ""
    source: Optional[str] = None
    source_session_id: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class SessionOut(BaseModel):
    session_id: UUID
    topic: str


class IngestIn(BaseModel):
    session_id: UUID
    speaker_id: Optional[str] = None
    speaker_type: Optional[ActorType] = None
    recipient_id: Optional[str] = None

    character_id: str
    user_name: str

    text: str
    kind: Optional[EventKind] = "chat_message"

    # raw "extra" fields; you can pass anything here and it lands in payload jsonb
    payload: Dict[str, Any] = Field(default_factory=dict)

    # optional for stable dedupe (recommended)
    dedupe_key: Optional[str] = None

    # optional: group chat, world instance, etc.
    scope_key: Optional[str] = None


class IngestOut(BaseModel):
    ok: bool
    event_id: int
    node_id: int
    timeline_id: UUID


class MemoryMatch(BaseModel):
    content: str
    score: float = 0.0
    meta: Dict[str, Any] = Field(default_factory=dict)


class MemoryOut(BaseModel):
    timeline_id: Optional[UUID] = None
    vector_matches: List[MemoryMatch] = Field(default_factory=list)
