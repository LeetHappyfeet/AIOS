"""Parse only host-authored research intent from an actual character source node.

A model-emitted <aios_action> is a REQUEST; no search, knowledge or world
write is performed by parsing the text. Never inspect another user's turn.
"""
from __future__ import annotations

import json
import re
from typing import Any

_ACTION = re.compile(r"<aios_action>(.{1,4000}?)</aios_action>",re.DOTALL|re.IGNORECASE)

def extract_research_request(source_text: str) -> str | None:
    matches=_ACTION.findall(str(source_text or ""))
    if not matches:
        return None
    if len(matches)!=1:
        raise ValueError("one research operation per source message")
    try:
        action=json.loads(matches[0])
    except (ValueError,TypeError) as exc:
        raise ValueError("malformed structured source action") from exc
    if not isinstance(action,dict):
        raise ValueError("source action must be a JSON object")
    if action.get("op")!="research":
        return None
    question=action.get("question")
    if not isinstance(question,str):
        raise ValueError("research question must be text")
    question=" ".join(question.split())
    if not 3<=len(question)<=600:
        raise ValueError("research question length must be 3-600")
    return question


def strip_tool_markup(text: Any) -> str:
    """Display helper; the raw source event remains unchanged for audit."""
    return _ACTION.sub("",str(text or "")).strip()
