from __future__ import annotations

import asyncio
import threading
from uuid import UUID

import gradio as gr

from aios_app.config import settings
from aios_app.db import Database
from aios_app.inference import InferenceBroker, InferenceProviderStore
from aios_app.inference.options import API_FORMATS, TOKEN_PARAMETERS, validate_options
from aios_app.ui.registry import register_tab


_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()

db = Database(settings.db_dsn)
run_async(db.connect())

PRESETS = {
    "Local / custom": "",
    "OpenAI": "https://api.openai.com/v1",
    "DeepSeek": "https://api.deepseek.com",
    "Mancer": "https://neuro.mancer.tech/oai/v1",
}

def _rows(providers=None):
    if providers is None:
        providers = run_async(InferenceProviderStore(db).list())
    return [[
        str(p.provider_id), p.provider_key, p.display_name, p.base_url, p.model,
        p.provider_type, p.status, p.enabled, p.drain, p.max_concurrency,
        p.context_window or "", ",".join(p.worker_classes),
        "strict" if p.strict_worker_classes else "preferred",
        p.consecutive_failures, p.protocol_failures, p.last_error or "",
    ] for p in providers]

def _preset(name):
    return PRESETS.get(str(name), "")

def _refresh(selected=None):
    providers = run_async(InferenceProviderStore(db).list())
    choices = [(f"{p.display_name} ({p.provider_key}) — {p.model}", str(p.provider_id)) for p in providers]
    selected = selected if any(value == selected for _, value in choices) else None
    return _rows(providers), gr.Dropdown(choices=choices, value=selected)


def _load(selected):
    if not selected:
        return ("", "", "", "", "", "", 1, 32768, 900, "", False, True,
                "Provider default", "auto", "default", "auto", 0, 0, True)
    provider = run_async(InferenceProviderStore(db).get(UUID(selected)))
    if provider is None:
        raise gr.Error("This worker no longer exists. Refresh the worker list.")
    caps = provider.capabilities
    thinking = {True: "Enabled", False: "Disabled", None: "Provider default"}[caps.get("thinking")]
    # Never send a saved credential back to the browser.
    return (provider.provider_key, provider.display_name, provider.base_url, provider.model,
            provider.api_key_env or "", "", provider.max_concurrency,
            provider.context_window or 0, provider.timeout_seconds,
            ",".join(provider.worker_classes), provider.strict_worker_classes,
            caps.get("json_mode", True), thinking, caps.get("api_format", "auto"),
            caps.get("reasoning_effort", "default"), caps.get("token_parameter", "auto"),
            caps.get("completion_budget", 0), caps.get("completion_budget_limit", 0),
            caps.get("retry_truncated", True))


def _save(key, name, url, model, api_env, api_key, concurrency, context, timeout, classes,
          strict, json_mode, thinking, api_format, effort, token_parameter, budget, budget_limit, retry):
    worker_classes = [v.strip() for v in str(classes or "").split(",") if v.strip()]
    store = InferenceProviderStore(db)
    existing = next((p for p in run_async(store.list()) if p.provider_key == str(key).strip()), None)
    caps = dict(existing.capabilities) if existing else {}
    caps.update({"json_mode": bool(json_mode),
                 "thinking": {"Enabled": True, "Disabled": False, "Provider default": None}[thinking],
                 "api_format": api_format, "reasoning_effort": effort,
                 "token_parameter": token_parameter, "completion_budget": budget or 0,
                 "completion_budget_limit": budget_limit or 0, "retry_truncated": bool(retry)})
    # Gradio Number can return floats even with precision=0; reject fractions.
    for field in ("completion_budget", "completion_budget_limit"):
        if int(caps[field]) != caps[field]:
            raise gr.Error("Completion budgets must be whole numbers.")
        caps[field] = int(caps[field])
    try:
        validate_options(caps)
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    provider = run_async(InferenceProviderStore(db).upsert(
        provider_key=str(key).strip(), display_name=str(name).strip(),
        base_url=str(url).strip(), model=str(model).strip(),
        api_key_env=str(api_env).strip() or None,
        api_key_secret=str(api_key).strip() or None,
        max_concurrency=int(concurrency or 1),
        context_window=int(context) if context else None,
        timeout_seconds=float(timeout or 900),
        worker_classes=worker_classes, strict_worker_classes=bool(strict),
        enabled=existing.enabled if existing else True,
        drain=existing.drain if existing else False,
        provider_type=existing.provider_type if existing else "openai_compatible",
        capabilities=caps,
    ))
    return f"Saved **{provider.display_name}**.", *_refresh(str(provider.provider_id))

def _control(provider_id, operation):
    if not provider_id:
        raise gr.Error("Select a worker first.")
    pid = UUID(str(provider_id).strip())
    store = InferenceProviderStore(db)
    if operation == "Enable":
        run_async(store.set_control(pid, enabled=True))
    elif operation == "Disable":
        run_async(store.set_control(pid, enabled=False))
    elif operation == "Drain":
        run_async(store.set_control(pid, drain=True))
    elif operation == "Resume":
        run_async(store.set_control(pid, drain=False))
    elif operation == "Remove":
        run_async(store.delete(pid))
        return "Worker removed.", *_refresh()
    elif operation == "Health check":
        ok = run_async(InferenceBroker(db).health_check(pid))
        return ("Health check passed (endpoint reachable; no generation tested)." if ok else "Health check failed."), *_refresh(provider_id)
    return f"{operation} applied.", *_refresh(provider_id)

@register_tab
def render():
    with gr.Tab("Inference Workers"):
        gr.Markdown(
            "### LLM inference pool\n"
            "Register local donated workers or OpenAI-compatible external services. "
            "Worker classes are preferences unless **Strict classes** is enabled, so idle "
            "capacity can accept other work. Direct API keys are stored in the AIOS database; "
            "use an environment-variable name instead when persistent secret storage is undesirable."
        )
        status = gr.Markdown()
        table = gr.Dataframe(
            headers=[
                "provider_id","key","name","base_url","model","type","status","enabled",
                "drain","concurrency","context","worker_classes","class_mode",
                "health_failures","protocol_failures","last_error",
            ], value=_rows(), interactive=False, wrap=True,
        )
        refresh = gr.Button("Refresh")

        with gr.Accordion("Add / update worker", open=False):
            preset = gr.Dropdown(list(PRESETS), value="Local / custom", label="Service preset")
            with gr.Row():
                key = gr.Textbox(label="Worker key", placeholder="gaming-pc-1")
                name = gr.Textbox(label="Display name", placeholder="Gaming PC 1")
                model = gr.Textbox(label="Model", placeholder="model identifier")
            url = gr.Textbox(label="OpenAI-compatible base URL", placeholder="http://host:8000/v1")
            with gr.Row():
                api_env = gr.Textbox(label="API key environment variable (preferred)")
                api_key = gr.Textbox(label="API key (optional)", type="password")
            with gr.Row():
                concurrency = gr.Number(label="Max concurrency", value=1, precision=0)
                context = gr.Number(label="Context window", value=32768, precision=0)
                timeout = gr.Number(label="Inference deadline seconds", value=900)
            classes = gr.Textbox(
                label="Preferred worker classes (comma-separated; blank = any)",
                placeholder="executive,research,planning",
            )
            strict = gr.Checkbox(label="Strict classes (never accept other task classes)", value=False)
            json_mode = gr.Checkbox(label="Supports response_format json_object", value=True)
            thinking = gr.Dropdown(["Provider default", "Enabled", "Disabled"],
                                   value="Provider default", label="Model thinking / reasoning")
            with gr.Row():
                api_format = gr.Dropdown(list(API_FORMATS), value="auto", label="Reasoning API format")
                effort = gr.Dropdown(["default", "low", "medium", "high", "max"],
                                     value="default", label="Reasoning effort (DeepSeek / OpenAI)")
                token_parameter = gr.Dropdown(list(TOKEN_PARAMETERS), value="auto", label="Completion token parameter")
            gr.Markdown(
                "Auto detects the direct DeepSeek/OpenAI API hostname; other endpoints retain local-server flags. "
                "For a proxy, select its supported format. Generic sends no thinking flags. "
                "The thinking switch applies to DeepSeek/local servers; OpenAI uses the effort setting. "
                "Completion tokens include reasoning where the provider counts it. A context window is a separate limit."
            )
            with gr.Row():
                budget = gr.Number(label="Completion budget (0 = automatic)", value=0, precision=0)
                budget_limit = gr.Number(label="Completion budget ceiling (0 = automatic)", value=0, precision=0)
            retry = gr.Checkbox(label="Retry truncated completion once, up to the ceiling", value=True)
            gr.Markdown("Automatic budgets allow at least 8192 tokens for recognized reasoning models; "
                        "the automatic retry ceiling is at least 16384. Retries consume additional tokens. "
                        "A blank API key preserves the existing saved key when updating the same worker key.")
            save = gr.Button("Save worker", variant="primary")
            preset.change(fn=_preset, inputs=preset, outputs=url)

        with gr.Accordion("Worker controls", open=True):
            providers = run_async(InferenceProviderStore(db).list())
            provider_id = gr.Dropdown(
                choices=[(f"{p.display_name} ({p.provider_key}) — {p.model}", str(p.provider_id)) for p in providers],
                label="Worker", value=None,
            )
            load = gr.Button("Load selected worker into editor")
            new = gr.Button("Clear editor for a new worker")
            operation = gr.Radio(
                ["Health check","Enable","Disable","Drain","Resume","Remove"],
                value="Health check", label="Operation",
            )
            apply = gr.Button("Apply")

        editor = [key,name,url,model,api_env,api_key,concurrency,context,timeout,classes,strict,
                  json_mode,thinking,api_format,effort,token_parameter,budget,budget_limit,retry]
        refresh.click(fn=_refresh, inputs=provider_id, outputs=[table,provider_id])
        load.click(fn=_load, inputs=provider_id, outputs=editor)
        new.click(fn=lambda: _load(None), outputs=editor)
        save.click(
            fn=_save,
            inputs=editor,
            outputs=[status,table,provider_id],
        )
        apply.click(fn=_control, inputs=[provider_id,operation], outputs=[status,table,provider_id])
