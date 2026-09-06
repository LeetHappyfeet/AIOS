from __future__ import annotations

import logging
from typing import Optional

from aios_app.db import Database
from aios_app.char.identity_store import IdentityStore
from aios_app.world.topology import ensure_character_root_world

logger = logging.getLogger("aios.char.discover")


async def run_worker(
    db: Database,
    *,
    character_id: str,
) -> None:
    """
    Ensure a character_identity row exists for a character_id
    observed in ingest_event.

    This is PURELY structural.
    """

    store = IdentityStore(db)

    existing = await store.get(character_id)
    if existing:
        await ensure_character_root_world(db, character_id=character_id)
        logger.debug("Character %s already registered", character_id)
        return

    logger.info("Discovered new character_id: %s", character_id)

    # Minimal bootstrap — no assumptions
    await store.create(
        character_id=character_id,
        display_name=character_id,
        canonical_name=character_id,
        meta={
            "source": "ingest_event",
            "bootstrap": "auto",
        },
    )

    await ensure_character_root_world(db, character_id=character_id)

    logger.info("Character %s registered in character_identity with root world", character_id)
