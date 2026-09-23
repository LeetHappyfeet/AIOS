from __future__ import annotations

import json
from typing import Any

from aios_app.db import Database
from aios_app.char.identity_kernel import _json_value


IDENTITY_RELATIONSHIPS = frozenset({"native", "crossover"})


def _domain_value(facet_key: str, value: Any) -> tuple[str, str] | None:
    value = _json_value(value)
    if not isinstance(value, dict):
        return None
    domain = str(value.get("domain") or facet_key).strip()
    relationship = str(value.get("relationship") or "native").strip().lower()
    if not domain or relationship not in IDENTITY_RELATIONSHIPS:
        return None
    return domain, relationship


class CharacterDomainProjector:
    """Project authoritative identity domain facets into corpus affinity rows.

    This is deliberately one-way. Corpus documents, search hits, and exposures
    never feed identity or native/crossover affiliation.
    """

    def __init__(self, db: Database):
        self.db = db

    async def reconcile(self, character_id: str) -> dict[str, Any]:
        facets = await self.db.fetch(
            """SELECT facet_id, facet_key, value, authority
               FROM aios.character_identity_facet
               WHERE character_id=$1 AND facet_type='domain' AND status='active'
               ORDER BY facet_key""",
            character_id,
        )
        desired: dict[str, tuple[str, Any]] = {}
        for facet in facets:
            parsed = _domain_value(str(facet["facet_key"]), facet["value"])
            if not parsed:
                continue
            domain, relationship = parsed
            desired[domain] = (relationship, facet["facet_id"])

        # Only rows owned by this projector are replaced. Acquired/study and
        # explicit operator grants remain independent of identity revisions.
        await self.db.execute(
            """DELETE FROM aios.character_knowledge_domain
               WHERE character_id=$1 AND provenance='identity_facet'
                 AND NOT (knowledge_domain = ANY($2::text[]))""",
            character_id,
            sorted(desired),
        )
        for domain, (relationship, facet_id) in desired.items():
            await self.db.execute(
                """INSERT INTO aios.character_knowledge_domain (
                       character_id, knowledge_domain, enabled, relationship,
                       provenance, source_facet_id, meta
                   )
                   VALUES ($1,$2,TRUE,$3,'identity_facet',$4,$5::jsonb)
                   ON CONFLICT (character_id, knowledge_domain) DO UPDATE
                   SET enabled=TRUE,
                       relationship=EXCLUDED.relationship,
                       provenance='identity_facet',
                       source_facet_id=EXCLUDED.source_facet_id,
                       meta=aios.character_knowledge_domain.meta || EXCLUDED.meta,
                       updated_at=now()""",
                character_id, domain, relationship, facet_id,
                json.dumps({"projected_from": "character_identity_facet"}),
            )
        return {
            "character_id": character_id,
            "identity_domains": sorted(desired),
            "count": len(desired),
        }
