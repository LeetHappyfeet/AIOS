# aios_app/rdf/character_writer.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

from .fuseki import FusekiClient, FusekiError

logger = logging.getLogger("aios.rdf.character_writer")


@dataclass
class CharacterWriteContext:
    dataset: str
    character_id: str
    world_id: UUID
    timeline_id: UUID
    node_id: UUID
    event_id: int
    speaker_type: Optional[str] = None
    speaker_id: Optional[str] = None
    recipient_id: Optional[str] = None
    message_text: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


def _graph_iri(character_id: str) -> str:
    safe = character_id.replace(" ", "_")
    return f"urn:aios:graph:character:{safe}"


def _iri(prefix: str, value: str) -> str:
    safe = value.replace(" ", "_")
    return f"urn:aios:{prefix}:{safe}"


def _lit(s: Optional[str]) -> str:
    if s is None:
        return '""'
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_character_event(fuseki: FusekiClient, ctx: CharacterWriteContext) -> bool:
    """
    Sync function (so it can be run in a worker thread).
    Returns True on success, False on failure.
    """
    g = _graph_iri(ctx.character_id)
    char = _iri("character", ctx.character_id)
    node = f"urn:aios:node:{ctx.node_id}"
    tl = f"urn:aios:timeline:{ctx.timeline_id}"
    world = f"urn:aios:world:{ctx.world_id}"
    ev = f"urn:aios:event:{ctx.event_id}"

    sparql = f"""
PREFIX aios: <urn:aios:>
INSERT DATA {{
  GRAPH <{g}> {{
    <{char}> a aios:Character .
    <{world}> a aios:World .
    <{tl}> a aios:Timeline ;
          aios:inWorld <{world}> ;
          aios:hasNode <{node}> .

    <{node}> a aios:DagNode ;
          aios:event <{ev}> ;
          aios:message {_lit(ctx.message_text)} ;
          aios:speakerType {_lit(ctx.speaker_type)} ;
          aios:speakerId {_lit(ctx.speaker_id)} ;
          aios:recipientId {_lit(ctx.recipient_id)} .

    <{char}> aios:hasTimeline <{tl}> .
  }}
}}
""".strip()

    try:
        fuseki.update(ctx.dataset, sparql)
        return True
    except FusekiError as e:
        logger.exception("FusekiError writing character event: %s", e)
        return False
    except Exception as e:
        logger.exception("Unexpected error writing character event: %s", e)
        return False
