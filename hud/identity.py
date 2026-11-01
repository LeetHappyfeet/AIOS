"""Delivery-scoped identity projection; never modify the compiled identity cache."""
from datetime import datetime, timezone
import re

from aios_app.char.identity_kernel import presentation_facets, _text_value
from aios_app.hud.text_budget import excerpt


def project_identity(identity, *, user_name=None, as_of=None, idle_duration=None, max_chars=None):
    name = identity.get("display_name") or identity.get("canonical_name") or identity.get("character_id") or "unknown"
    variables = {
        "char": str(name), "user": str(user_name or "unknown user"),
        "isodate": (as_of or datetime.now(timezone.utc)).date().isoformat(),
        "idle_duration": str(idle_duration or "unknown"),
    }
    def resolve(text):
        return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}",
                      lambda match: variables.get(match[1].casefold(), "unknown"), str(text))

    facets = identity.get("identity_facets") or {}
    original = str(identity.get("identity_kernel") or "")
    if max_chars is None or not facets:
        text = resolve(original)
        return text if max_chars is None else excerpt(text, max_chars)

    # Share space across authored facets. A lengthy clothing list must not
    # displace personality or constraints. Excerpts are explicitly marked.
    groups = presentation_facets(facets)
    order = ["constraint", "values", "personality", "identity", "role",
             "appearance", "description", "expression", "developmental"]
    kinds = sorted(groups, key=lambda kind: (order.index(kind) if kind in order else len(order), kind))
    baseline = []
    for key in ("species", "primary_role", "moral_constraints", "default_tone", "speech_style"):
        if identity.get(key):
            baseline.append(f"{key.replace('_', ' ')}: {_text_value(identity[key])}")
    entries = (["AUTHORED BASELINE: " + " | ".join(baseline)] if baseline else [])
    for kind in kinds:
        values = sorted(groups[kind], key=lambda item: item["key"])
        text = " | ".join(_text_value(item["value"]) for item in values if _text_value(item["value"]))
        if text:
            entries.append(kind.upper() + ": " + text)
    heading = "IDENTITY KERNEL — " + str(name)
    remaining = max(0, max_chars - len(heading) - 1)
    lines = [heading]
    for index, entry in enumerate(entries):
        share = remaining // (len(entries) - index)
        if share < 40:
            lines.append(excerpt("[additional identity details omitted]", remaining))
            break
        rendered = excerpt(resolve(entry), share)
        lines.append(rendered)
        remaining -= len(rendered) + 1
    return "\n".join(lines)[:max_chars]
