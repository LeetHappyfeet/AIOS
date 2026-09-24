from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import UUID

from aios_app.char.identity_kernel import IdentityKernelStore
from aios_app.db import Database
from aios_app.hud.context import HUDContextResolver
from aios_app.hud.frame import HUDAssembler
from aios_app.hud.worker_profile import get_worker_hud_profile


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


def _item_text(item: Mapping[str, Any]) -> str:
    return _clip(
        item.get("text")
        or item.get("canonical_text")
        or item.get("message_text")
        or item.get("label")
        or "",
        260,
    )


class InternalHUDAssembler:
    """Render a small, purpose-specific HUD from cognition already resolved by AIOS.

    The worker never performs its own retrieval. HUDAssembler/CognitiveContextService
    owns branch-safe recall and relevance selection; this layer projects only the
    few items useful to the selected faculty and enforces a hard micro-prompt cap.
    """

    def __init__(self, db: Database):
        self.db = db
        self.contexts = HUDContextResolver(db)
        self.identities = IdentityKernelStore(db)
        self.foreground = HUDAssembler(db)

    async def build(
        self, instance_id: UUID, *, clues: Sequence[str],
        candidates: Sequence[Mapping[str, Any]], token_budget: int | None = None,
        worker_profile: str = "attention", focus_text: str | None = None,
        subject: str | None = None,
    ) -> InternalHUD:
        context = await self.contexts.resolve(instance_id)
        profile = get_worker_hud_profile(worker_profile)
        resolved_budget = max(180, min(int(token_budget or profile.token_budget), 700))

        kernel = await self.identities.get(context.character_id)
        core = dict(kernel.kernel_json.get("core") or {}) if kernel else {}
        name = core.get("display_name") or core.get("canonical_name") or context.character_id
        persona_bits = [
            core.get("species"), core.get("archetype"), core.get("default_tone"),
            core.get("speech_style"),
        ]
        persona = ", ".join(_clip(v, 56) for v in persona_bits if v)
        identity = f"I am {name}." + (f" {persona}." if persona else "")

        clue_lines = [_clip(v, 220) for v in list(clues)[:2] if _clip(v, 220)]
        resolved_focus = _clip(
            focus_text or subject or (clue_lines[0] if clue_lines else ""), 420
        )

        # Reuse the canonical cognition/relevance pipeline.  A small presentation
        # budget keeps this projection cheap; retrieval depth remains cognition-owned.
        frame = await self.foreground.build(
            instance_id,
            recent_limit=max(1, profile.recent_event_items),
            token_budget=max(resolved_budget, 320),
            focus_text=resolved_focus or None,
        )

        lines = [identity]
        if subject:
            lines.extend(["SUBJECT:", _clip(subject, 260)])

        scene = frame.get("scene") or {}
        working = scene.get("working_state") or {}
        scene_change = (
            working.get("last_significant_change")
            if isinstance(working, Mapping)
            else None
        ) or scene.get("last_significant_change")
        if profile.include_scene and scene_change:
            lines.extend(["CURRENT:", _clip(scene_change, 300)])

        if clue_lines:
            lines.append("CLUES:")
            lines.extend(f"- {value}" for value in clue_lines)

        def add_items(title: str, values: Sequence[Mapping[str, Any]], limit: int) -> None:
            texts = [_item_text(item) for item in list(values)[:max(0, limit)]]
            texts = [value for value in texts if value]
            if texts:
                lines.append(title + ":")
                lines.extend(f"- {value}" for value in texts)

        if profile.include_memories:
            add_items("RELEVANT MEMORY", frame.get("memories") or [], profile.memory_items)
        if profile.include_beliefs:
            add_items("KNOWN / BELIEVED", frame.get("beliefs") or [], profile.belief_items)
        if profile.include_goals:
            add_items("ACTIVE GOAL", frame.get("goals") or [], profile.goal_items)

        lines.append("CHOOSE ONE:")
        for item in candidates:
            lines.append(f"{item['key']}. {_clip(item['label'], 180)}")
        lines.append('Return JSON only: {"choice":"A"}')

        prompt = "\n".join(lines)
        # Character approximation is intentional and matches the foreground HUD's
        # inexpensive budgeting convention. Keep room for the complete choice set.
        prompt = prompt[: resolved_budget * 4]
        fingerprint = hashlib.sha256(
            json.dumps({
                "instance": str(instance_id),
                "state": context.state_version,
                "timeline": str(context.source_timeline_id or ""),
                "node": str(context.source_head_node_id or ""),
                "worker_profile": profile.name,
                "focus": resolved_focus,
                "subject": subject or "",
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
