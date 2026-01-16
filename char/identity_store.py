from __future__ import annotations

import logging
from typing import Dict, Optional, Iterable
from dataclasses import dataclass
from datetime import datetime

from aios_app.db import Database

logger = logging.getLogger("aios.char.identity_store")


# ============================================================
# Identity model (read-only projection)
# ============================================================

@dataclass(frozen=True)
class CharacterIdentity:
    """
    Canonical, slow-changing identity record.

    This is NOT:
      - beliefs
      - memory
      - mood
      - runtime state

    Think: driver's license / passport.
    """
    character_id: str
    canonical_name: Optional[str]
    display_name: Optional[str]
    canon: Optional[str]
    franchise: Optional[str]
    entity_type: str
    species: Optional[str]
    gender: Optional[str]
    age_descriptor: Optional[str]
    visual_summary: Optional[str]
    primary_role: Optional[str]
    archetype: Optional[str]
    default_tone: Optional[list[str]]
    speech_style: Optional[str]
    content_rating: Optional[str]
    moral_constraints: Optional[list[str]]
    is_canonical: bool
    is_mutable: bool
    process_ontology: bool
    home_world_id: Optional[str]
    meta: dict
    created_at: datetime
    updated_at: Optional[datetime]


# ============================================================
# Identity store
# ============================================================

class IdentityStore:
    """
    SQL-backed identity registry with optional in-memory caching.

    Safe to use:
      - in API handlers
      - in supervisor
      - in prompt builders
      - in RDF writers (read-only)
    """

    def __init__(self, db: Database, *, enable_cache: bool = True):
        self.db = db
        self.enable_cache = enable_cache
        self._cache: Dict[str, CharacterIdentity] = {}

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    async def get(self, character_id: str) -> Optional[CharacterIdentity]:
        """
        Load a character identity by ID.

        Returns None if not found.
        """
        if self.enable_cache and character_id in self._cache:
            return self._cache[character_id]

        row = await self.db.fetchrow(
            """
            SELECT *
            FROM aios.character_identity
            WHERE character_id = $1
            """,
            character_id,
        )

        if not row:
            return None

        identity = self._row_to_identity(row)

        if self.enable_cache:
            self._cache[character_id] = identity

        return identity

    async def list(
        self,
        *,
        canon: Optional[str] = None,
        franchise: Optional[str] = None,
        archetype: Optional[str] = None,
        entity_type: Optional[str] = None,
        limit: int = 100,
    ) -> Iterable[CharacterIdentity]:
        """
        List character identities with optional filtering.
        """
        clauses = []
        params = []
        idx = 1

        def add_clause(sql: str, value):
            nonlocal idx
            clauses.append(sql.replace("?", f"${idx}"))
            params.append(value)
            idx += 1

        if canon:
            add_clause("canon = ?", canon)
        if franchise:
            add_clause("franchise = ?", franchise)
        if archetype:
            add_clause("archetype = ?", archetype)
        if entity_type:
            add_clause("entity_type = ?", entity_type)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = await self.db.fetch(
            f"""
            SELECT *
            FROM aios.character_identity
            {where}
            ORDER BY canonical_name NULLS LAST
            LIMIT {limit}
            """,
            *params,
        )

        identities = [self._row_to_identity(r) for r in rows]

        if self.enable_cache:
            for ident in identities:
                self._cache[ident.character_id] = ident

        return identities

    async def exists(self, character_id: str) -> bool:
        """
        Lightweight existence check.
        """
        row = await self.db.fetchrow(
            "SELECT 1 FROM aios.character_identity WHERE character_id = $1",
            character_id,
        )
        return bool(row)

    async def create(
        self,
        *,
        character_id: str,
        canonical_name: Optional[str] = None,
        display_name: Optional[str] = None,
        canon: Optional[str] = None,
        franchise: Optional[str] = None,
        entity_type: str = "character",
        meta: Optional[dict] = None,
        home_world_id: Optional[str] = None,
    ) -> CharacterIdentity:
        """
        Create a new identity record.

        This should be called:
          - when importing a character card
          - when registering a new OC
          - NOT during runtime chat
        """
        meta = meta or {}

        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.character_identity (
                character_id,
                canonical_name,
                display_name,
                canon,
                franchise,
                entity_type,
                home_world_id,
                meta
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
            RETURNING *
            """,
            character_id,
            canonical_name,
            display_name,
            canon,
            franchise,
            entity_type,
            home_world_id,
            meta,
        )

        identity = self._row_to_identity(row)

        if self.enable_cache:
            self._cache[character_id] = identity

        logger.info("Registered new character identity '%s'", character_id)
        return identity

    def invalidate(self, character_id: str) -> None:
        """
        Remove a character from cache (safe no-op).
        """
        self._cache.pop(character_id, None)

    def clear_cache(self) -> None:
        """
        Clear identity cache (safe for hot reloads).
        """
        self._cache.clear()

    # --------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------

    @staticmethod
    def _row_to_identity(row) -> CharacterIdentity:
        return CharacterIdentity(
            character_id=row["character_id"],
            canonical_name=row.get("canonical_name"),
            display_name=row.get("display_name"),
            canon=row.get("canon"),
            franchise=row.get("franchise"),
            entity_type=row["entity_type"],
            species=row.get("species"),
            gender=row.get("gender"),
            age_descriptor=row.get("age_descriptor"),
            visual_summary=row.get("visual_summary"),
            primary_role=row.get("primary_role"),
            archetype=row.get("archetype"),
            default_tone=row.get("default_tone"),
            speech_style=row.get("speech_style"),
            content_rating=row.get("content_rating"),
            moral_constraints=row.get("moral_constraints"),
            is_canonical=row.get("is_canonical", True),
            is_mutable=row.get("is_mutable", False),
            process_ontology=row["process_ontology"],
            home_world_id=row.get("home_world_id"),
            meta=row["meta"] or {},
            created_at=row["created_at"],
            updated_at=row.get("updated_at"),
        )
