"""Provider wire options stored in the existing capabilities JSON column."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse


API_FORMATS = ("auto", "deepseek", "openai", "local", "generic")
TOKEN_PARAMETERS = ("auto", "max_tokens", "max_completion_tokens")


def validate_options(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    caps = dict(capabilities)
    if caps.get("api_format", "auto") not in API_FORMATS:
        raise ValueError("Unknown inference API format")
    if caps.get("token_parameter", "auto") not in TOKEN_PARAMETERS:
        raise ValueError("Unknown completion token parameter")
    if caps.get("thinking") is not None and not isinstance(caps["thinking"], bool):
        raise ValueError("Thinking must be true, false, or null (provider default)")
    if caps.get("reasoning_effort", "default") not in ("default", "low", "medium", "high", "max"):
        raise ValueError("Unknown reasoning effort")
    for key in ("completion_budget", "completion_budget_limit"):
        value = caps.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a nonnegative integer (0 = automatic)")
    if (caps.get("completion_budget") and caps.get("completion_budget_limit")
            and caps["completion_budget"] > caps["completion_budget_limit"]):
        raise ValueError("Completion budget exceeds the completion budget limit")
    return caps


def api_format(provider) -> str:
    configured = provider.capabilities.get("api_format", "auto")
    if configured != "auto":
        return configured
    host = (urlparse(provider.base_url).hostname or "").lower()
    if host == "api.deepseek.com":
        return "deepseek"
    if host == "api.openai.com":
        return "openai"
    # Preserve the existing local-server thinking flags for older workers.
    # Proxies that reject these can explicitly select generic or their dialect.
    return "local"


def reasoning_model(model: str) -> bool:
    model = model.lower().rsplit("/", 1)[-1]
    return ("reasoner" in model or "deepseek-r1" in model
            or model.startswith(("o1", "o3", "o4", "gpt-5")))


def completion_options(provider, requested_tokens: int) -> tuple[dict, str, int, int]:
    caps = validate_options(provider.capabilities)
    dialect = api_format(provider)
    thinking = caps.get("thinking")
    effort = caps.get("reasoning_effort", "default")
    reasoning = (thinking is True or reasoning_model(provider.model)
                 or (dialect == "deepseek" and thinking is None)
                 or (effort != "default" and (dialect == "openai" or thinking is not False)))
    fields: dict[str, Any] = {}
    if dialect == "deepseek":
        if thinking is not None:
            fields["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if thinking is not False and effort != "default":
            fields["reasoning_effort"] = effort
    elif dialect == "openai":
        if effort != "default":
            fields["reasoning_effort"] = effort
        # OpenAI models differ in support for disabling reasoning; do not guess
        # an unsupported 'none' effort for older reasoning-only models.
    elif dialect == "local" and thinking is not None:
        fields["enable_thinking"] = thinking
        fields["chat_template_kwargs"] = {"enable_thinking": thinking}
    token_parameter = caps.get("token_parameter", "auto")
    if token_parameter == "auto":
        token_parameter = ("max_completion_tokens" if dialect == "openai" and reasoning_model(provider.model)
                           else "max_tokens")
    initial = caps.get("completion_budget") or max(requested_tokens, 8192 if reasoning else 1)
    limit = caps.get("completion_budget_limit") or max(initial, 16384)
    return fields, token_parameter, min(initial, limit), limit
