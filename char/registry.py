from __future__ import annotations

import logging
from typing import Dict, Optional, Iterable
from dataclasses import dataclass

from aios_app.db import Database
from .identity_store import IdentityStore, CharacterIdentity

logger = logging.getLogger("aios.char.registry")


# ============================================================
# Registry errors
# ============================================================

class CharacterResolutionError(RuntimeError):
    pass


# ============================================================
# Character registry
# ============================================================

class CharacterRegistry:
    """
    Canonical character resolver.

    Responsibilities:
      - resolve character aliases → canonical character_id
      - load CharacterIdentity via IdentityStore
      - provide stable lookup surface for the rest of the system

    Non-responsibilities:
      - belief inference
      - ontology updates
      - RDF writes
      - memory
      - prompt assembly
    """

    def __init__(
        self,
        db: Database,
        *,
        identity_store: Optional[IdentityStore] = None,
        enable_cache: bool = True,
    ):
        self.db = db
        self.identity_store = identity_store or IdentityStore(
            db,
            enable_cache=enable_cache,
        )

        # alias → character_id cache
        self._alias_cache: Dict[str, str] = {}

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    async def resolve(
        self,
        identifier: str,
        *,
        create_if_missing: bool = False,
        defaults: Optional[dict] = None,
    ) -> CharacterIdentity:
        """
        Resolve any identifier into a CharacterIdentity.

        identifier may be:
          - canonical character_id
          - display name
          - alias
          - external handle (e.g. SillyTavern name)

        If create_if_missing=True:
          - a new identity is created using defaults
        """
        identifier = identifier.strip()

        # 1) Fast path: cache
        cached = self._alias_cache.get(identifier)
        if cached:
            ident = await self.identity_store.get(cached)
            if ident:
                return ident

        # 2) Direct character_id match
        ident = await self.identity_store.get(identifier)
        if ident:
            self._alias_cache[identifier] = ident.character_id
            return ident

        # 3) Alias table lookup
        row = await self.db.fetchrow(
            """
            SELECT character_id
            FROM aios.character_alias
            WHERE alias = $1
            """,
            identifier,
        )

        if row:
            character_id = row["character_id"]
            ident = await self.identity_store.get(character_id)
            if not ident:
                raise CharacterResolutionError(
                    f"Alias '{identifier}' points to missing character_id '{character_id}'"
                )

            self._alias_cache[identifier] = character_id
            return ident

        # 4) Display name match (slow but useful)
        row = await self.db.fetchrow(
            """
            SELECT character_id
            FROM aios.character_identity
            WHERE display_name = $1
            """,
            identifier,
        )

        if row:
            character_id = row["character_id"]
            ident = await self.identity_store.get(character_id)
            self._alias_cache[identifier] = character_id
            return ident

        # 5) Create new identity (optional)
        if create_if_missing:
            defaults = defaults or {}
            character_id = defaults.get("character_id") or identifier

            ident = await self.identity_store.create(
                character_id=character_id,
                canonical_name=defaults.get("canonical_name"),
                display_name=defaults.get("display_name") or identifier,
                canon=defaults.get("canon"),
                franchise=defaults.get("franchise"),
                entity_type=defaults.get("entity_type", "character"),
                home_world_id=defaults.get("home_world_id"),
                meta=defaults.get("meta", {}),
            )

            # self-alias
            await self.add_alias(
                alias=identifier,
                character_id=ident.character_id,
                is_primary=True,
            )

            self._alias_cache[identifier] = ident.character_id
            return ident

        raise CharacterResolutionError(
            f"Unable to resolve character identifier '{identifier}'"
        )

    async def add_alias(
        self,
        *,
        alias: str,
        character_id: str,
        is_primary: bool = False,
        source: Optional[str] = None,
    ) -> None:
        """
        Register an alias for a character.

        Examples:
          - 'Mrs Frizzle'
          - 'Ms. Frizzle'
          - 'frizzle'
          - 'mrs_frizzle'
        """
        await self.db.execute(
            """
            INSERT INTO aios.character_alias (
                alias,
                character_id,
                is_primary,
                source
            )
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (alias) DO UPDATE
              SET character_id = EXCLUDED.character_id
            """,
            alias.strip(),
            character_id,
            is_primary,
            source,
        )

        self._alias_cache[alias.strip()] = character_id

    async def list_aliases(self, character_id: str) -> Dict[str, bool]:
        """
        Return aliases for a character_id → {alias: is_primary}
        """
        rows = await self.db.fetch(
            """
            SELECT alias, is_primary
            FROM aios.character_alias
            WHERE character_id = $1
            """,
            character_id,
        )

        return {r["alias"]: r["is_primary"] for r in rows}

    async def invalidate(self, character_id: str) -> None:
        """
        Clear caches for a character.
        """
        self.identity_store.invalidate(character_id)

        for alias, cid in list(self._alias_cache.items()):
            if cid == character_id:
                self._alias_cache.pop(alias, None)

    def clear_cache(self) -> None:
        """
        Clear all registry caches.
        """
        self.identity_store.clear_cache()
        self._alias_cache.clear()
