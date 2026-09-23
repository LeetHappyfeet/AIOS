from __future__ import annotations

import asyncio
import threading
from uuid import UUID

import gradio as gr

from aios_app.config import settings
from aios_app.db import Database
from aios_app.inference import InferenceBroker, InferenceProviderStore
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

def _rows():
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

def _save(key, name, url, model, api_env, api_key, concurrency, context, timeout, classes, strict, json_mode):
    worker_classes = [v.strip() for v in str(classes or "").split(",") if v.strip()]
    provider = run_async(InferenceProviderStore(db).upsert(
        provider_key=str(key).strip(), display_name=str(name).strip(),
        base_url=str(url).strip(), model=str(model).strip(),
        api_key_env=str(api_env).strip() or None,
        api_key_secret=str(api_key).strip() or None,
        max_concurrency=int(concurrency or 1),
        context_window=int(context) if context else None,
        timeout_seconds=float(timeout or 900),
        worker_classes=worker_classes, strict_worker_classes=bool(strict),
        capabilities={"json_mode": bool(json_mode)},
    ))
    return f"Saved **{provider.display_name}**.", _rows()

def _control(provider_id, operation):
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
        return "Worker removed.", _rows()
    elif operation == "Health check":
        ok = run_async(InferenceBroker(db).health_check(pid))
        return ("Health check passed." if ok else "Health check failed."), _rows()
    return f"{operation} applied.", _rows()

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
            save = gr.Button("Save worker", variant="primary")
            preset.change(fn=_preset, inputs=preset, outputs=url)

        with gr.Accordion("Worker controls", open=True):
            provider_id = gr.Textbox(label="Provider ID")
            operation = gr.Radio(
                ["Health check","Enable","Disable","Drain","Resume","Remove"],
                value="Health check", label="Operation",
            )
            apply = gr.Button("Apply")

        refresh.click(fn=_rows, outputs=table)
        save.click(
            fn=_save,
            inputs=[key,name,url,model,api_env,api_key,concurrency,context,timeout,classes,strict,json_mode],
            outputs=[status,table],
        )
        apply.click(fn=_control, inputs=[provider_id,operation], outputs=[status,table])
