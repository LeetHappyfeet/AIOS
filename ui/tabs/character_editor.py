# aios_app/ui/tabs/character_editor.py

from __future__ import annotations

import asyncio
import threading
import gradio as gr

from aios_app.ui.registry import register_tab
from aios_app.db import Database
from aios_app.config import settings

# -------------------------------------------------
# Background async loop for DB access
# -------------------------------------------------

_async_loop = asyncio.new_event_loop()
_thread = threading.Thread(target=_async_loop.run_forever, daemon=True)
_thread.start()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _async_loop).result()

# -------------------------------------------------
# DB (connect once)
# -------------------------------------------------

db = Database(settings.db_dsn)
run_async(db.connect())

# -------------------------------------------------
# Background async loop for DB access
# -------------------------------------------------

_async_loop = asyncio.new_event_loop()
_thread = threading.Thread(
    target=_async_loop.run_forever,
    daemon=True,
)
_thread.start()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _async_loop).result()


# -------------------------------------------------
# Async helpers (DB access)
# -------------------------------------------------

async def _load_character(cid: str):
    return await db.fetchrow(
        "SELECT * FROM aios.character_identity WHERE character_id=$1",
        cid,
    )


async def _save_character(
    cid: str,
    display_name: str,
    canon: str,
    franchise: str,
    entity_type: str,
    species: str,
    gender: str,
    archetype: str,
    primary_role: str,
    default_tone: list[str],
    speech_style: str,
    is_canonical: bool,
    is_mutable: bool,
):
    await db.execute(
        """
        UPDATE aios.character_identity
        SET
            display_name      = $2,
            canon             = $3,
            franchise         = $4,
            entity_type       = $5,
            species           = $6,
            gender            = $7,
            archetype         = $8,
            primary_role      = $9,
            default_tone      = $10,
            speech_style      = $11,
            is_canonical      = $12,
            is_mutable        = $13,
            updated_at        = now()
        WHERE character_id = $1
        """,
        cid,
        display_name or None,
        canon or None,
        franchise or None,
        entity_type or "character",
        species or None,
        gender or None,
        archetype or None,
        primary_role or None,
        default_tone or None,
        speech_style or None,
        is_canonical,
        is_mutable,
    )


# -------------------------------------------------
# Gradio tab
# -------------------------------------------------

@register_tab
def render():
    with gr.Tab("Character Editor"):

        gr.Markdown(
            """
            ### Character Identity Editor  
            Edits **only** `aios.character_identity`.  
            No memory, no RDF, no DAG mutations.
            """
        )

        # -----------------------------
        # Identity
        # -----------------------------

        with gr.Group():
            char_id = gr.Textbox(
                label="character_id",
                placeholder="e.g. Renamon",
            )

            load_status = gr.Markdown()

        # -----------------------------
        # Core identity
        # -----------------------------

        with gr.Row():
            display_name = gr.Textbox(label="display_name")
            entity_type = gr.Textbox(label="entity_type", value="character")

        # -----------------------------
        # Canon / franchise
        # -----------------------------

        with gr.Row():
            canon = gr.Textbox(label="canon")
            franchise = gr.Textbox(label="franchise")

        # -----------------------------
        # Description
        # -----------------------------

        with gr.Row():
            species = gr.Textbox(label="species")
            gender = gr.Textbox(label="gender")

        archetype = gr.Textbox(label="archetype")
        primary_role = gr.Textbox(label="primary_role")

        # -----------------------------
        # Behavioral defaults
        # -----------------------------

        default_tone = gr.CheckboxGroup(
            label="default_tone",
            choices=[
                "calm",
                "playful",
                "sarcastic",
                "formal",
                "aggressive",
                "empathetic",
                "curious",
            ],
        )

        speech_style = gr.Textbox(label="speech_style")

        # -----------------------------
        # Flags
        # -----------------------------

        with gr.Row():
            is_canonical = gr.Checkbox(label="is_canonical", value=True)
            is_mutable = gr.Checkbox(label="is_mutable", value=False)

        save_status = gr.Markdown()

        # -------------------------------------------------
        # Button handlers (SYNC wrappers)
        # -------------------------------------------------

        def load_click(cid: str):
            if not cid:
                return [""] * 11 + ["❌ character_id required"]

            row = run_async(_load_character(cid))

            if not row:
                return [""] * 11 + ["❌ Character not found"]

            return [
                row["display_name"],
                row["entity_type"],
                row["canon"],
                row["franchise"],
                row["species"],
                row["gender"],
                row["archetype"],
                row["primary_role"],
                row["default_tone"] or [],
                row["speech_style"],
                row["is_canonical"],
                row["is_mutable"],
                "✅ Loaded",
            ]

        def save_click(
            cid,
            display,
            entity_type,
            canon,
            franchise,
            species,
            gender,
            archetype,
            primary_role,
            default_tone,
            speech_style,
            is_canonical,
            is_mutable,
        ):
            if not cid:
                return "❌ character_id required"

            
            run_async(
                _save_character(
                    cid,
                    display,
                    canon,
                    franchise,
                    entity_type,
                    species,
                    gender,
                    archetype,
                    primary_role,
                    default_tone,
                    speech_style,
                    is_canonical,
                    is_mutable,
                )
            )


            return "💾 Saved"

        # -------------------------------------------------
        # Wiring
        # -------------------------------------------------

        gr.Button("Load").click(
            fn=load_click,
            inputs=char_id,
            outputs=[
                display_name,
                entity_type,
                canon,
                franchise,
                species,
                gender,
                archetype,
                primary_role,
                default_tone,
                speech_style,
                is_canonical,
                is_mutable,
                load_status,
            ],
        )

        gr.Button("Save").click(
            fn=save_click,
            inputs=[
                char_id,
                display_name,
                entity_type,
                canon,
                franchise,
                species,
                gender,
                archetype,
                primary_role,
                default_tone,
                speech_style,
                is_canonical,
                is_mutable,
            ],
            outputs=save_status,
        )
