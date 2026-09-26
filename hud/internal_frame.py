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


def _scene_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return _clip(value.get("text") or value.get("label") or "", 300)
    return _clip(value, 300)


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
        facets = dict(kernel.kernel_json.get("facets") or {}) if kernel else {}
        persona_bits = [
            core.get("species"), core.get("primary_role"), core.get("archetype"),
            core.get("default_tone"), core.get("speech_style"),
        ]
        # Decision workers need a tiny authored roleplay anchor, not the full
        # foreground identity prompt. Personality/values/role/expression are
        # preferred because they affect choices; appearance is intentionally omitted.
        for facet_type in ("personality","values","role","expression","constraint"):
            for facet in list(facets.get(facet_type) or [])[:2]:
                value=facet.get("value") if isinstance(facet, Mapping) else None
                if isinstance(value, Mapping):
                    persona_bits.extend(value.values())
                elif isinstance(value, Sequence) and not isinstance(value, (str,bytes)):
                    persona_bits.extend(value)
                elif value:
                    persona_bits.append(value)
        persona = "; ".join(_clip(v, 72) for v in persona_bits if _clip(v,72))[:300]
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
            lines.extend(["CURRENT:", _scene_text(scene_change)])

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
            goals = list(frame.get("goals") or [])[:max(0, profile.goal_items)]
            add_items("ACTIVE GOAL", goals, profile.goal_items)
            # Planning workers get a tiny deterministic lifecycle digest. Other
            # workers keep the old text-only goal surface.
            if worker_profile == "planning" and goals:
                lifecycle = goals[0].get("lifecycle") or {}
                bits = []
                crossings = int(lifecycle.get("crossing_count") or 0)
                progress = int(lifecycle.get("progress_count") or 0)
                blockers = int(lifecycle.get("blocker_count") or 0)
                candidates = int(lifecycle.get("completion_candidate_count") or 0)
                if crossings:
                    bits.append(f"crossings={crossings}")
                if progress:
                    bits.append(f"progress={progress}")
                if blockers:
                    bits.append(f"blockers={blockers}")
                if candidates:
                    bits.append(f"completion_candidates={candidates}")
                if lifecycle.get("thread_status"):
                    bits.append(f"thread={lifecycle['thread_status']}")
                latest = lifecycle.get("latest_evidence") or {}
                if latest.get("relation"):
                    bits.append(f"latest={latest['relation']}")
                if bits:
                    lines.extend(["GOAL STATE:", "; ".join(bits)])

        choice_lines = ["CHOOSE ONE:"]
        for item in candidates:
            choice_lines.append(f"{item['key']}. {_clip(item['label'], 180)}")
        choice_lines.append('Return JSON only: {"choice":"A"}')

        # Never truncate the decision surface. Context is expendable; the complete
        # A-E choice set and response contract are not.
        suffix = "\n".join(choice_lines)
        char_budget = resolved_budget * 4
        context_budget = max(160, char_budget - len(suffix) - 1)
        prefix = "\n".join(lines)[:context_budget].rstrip()
        prompt = f"{prefix}\n{suffix}" if prefix else suffix
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
