from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import UUID

from aios_app.char.identity_kernel import IdentityKernelStore
from aios_app.db import Database
from aios_app.hud.context import HUDContextResolver


@dataclass(frozen=True)
class InternalHUD:
    prompt: str
    state_version: int
    source_timeline_id: UUID | None
    source_node_id: UUID | None
    fingerprint: str


def _clip(value: Any, chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:chars].rstrip()


class InternalHUDAssembler:
    """Build a tiny snapshot-bound HUD without semantic retrieval.

    This deliberately does not call HUDAssembler/CognitiveContextService. The
    worker receives identity, zero-to-two supplied clues, and prepared choices.
    """

    def __init__(self, db: Database):
        self.db = db
        self.contexts = HUDContextResolver(db)
        self.identities = IdentityKernelStore(db)

    async def build(
        self, instance_id: UUID, *, clues: Sequence[str],
        candidates: Sequence[Mapping[str, Any]], token_budget: int = 360,
    ) -> InternalHUD:
        context = await self.contexts.resolve(instance_id)
        kernel = await self.identities.get(context.character_id)
        core = dict(kernel.kernel_json.get("core") or {}) if kernel else {}
        name = core.get("display_name") or core.get("canonical_name") or context.character_id
        persona_bits = [
            core.get("species"), core.get("archetype"), core.get("default_tone"),
            core.get("speech_style"),
        ]
        persona = ", ".join(_clip(v, 72) for v in persona_bits if v)
        identity = f"I am {name}." + (f" {persona}." if persona else "")

        clue_lines = [_clip(v, 220) for v in list(clues)[:2] if _clip(v, 220)]
        lines = [
            identity,
            "This is one disposable internal decision. Use only the clues below; do not invent missing context.",
        ]
        if clue_lines:
            lines.append("CLUES:")
            lines.extend(f"- {v}" for v in clue_lines)
        else:
            lines.append("CLUES: none. Prefer WAIT unless a prepared choice is still clearly justified.")

        lines.append("CHOOSE ONE:")
        for item in candidates:
            lines.append(f"{item['key']}. {_clip(item['label'], 180)}")
        lines.extend([
            "Return JSON only: {\"choice\":\"A\",\"focus\":\"short reason or query\"}",
            "The choice is only a proposal. AIOS will recheck whether it is still timely before doing anything.",
        ])
        prompt = "\n".join(lines)
        # Hard character cap is intentional: this surface must stay micro even
        # when authored identity fields are unexpectedly verbose.
        prompt = prompt[: max(600, int(token_budget) * 4)]
        fingerprint = hashlib.sha256(
            json.dumps({
                "instance": str(instance_id), "state": context.state_version,
                "timeline": str(context.source_timeline_id or ""),
                "node": str(context.source_head_node_id or ""),
                "clues": clue_lines,
                "choices": [(str(x["key"]), str(x.get("operation"))) for x in candidates],
            }, sort_keys=True).encode()
        ).hexdigest()
        return InternalHUD(
            prompt=prompt, state_version=context.state_version,
            source_timeline_id=context.source_timeline_id,
            source_node_id=context.source_head_node_id,
            fingerprint=fingerprint,
        )
