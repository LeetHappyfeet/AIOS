from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable

from aios_app.corpus_adapters import CorpusClassification, CorpusFacet
from aios_app.db import Database


_WS_RE = re.compile(r"\s+")


def normalize_facet_value(value: str) -> str:
    """Canonical key used by both adapters and route rules."""
    return _WS_RE.sub(" ", str(value).strip().casefold())


@dataclass(frozen=True)
class CorpusFacetRoute:
    facet_type: str
    facet_value: str
    knowledge_domain: str
    scope_key: str
    epistemic_namespace: str
    access_class: str
    priority: int
    meta: dict


@dataclass(frozen=True)
class CorpusDocumentRoute:
    scopes: tuple[str, ...]
    knowledge_domains: tuple[str, ...]
    epistemic_namespace: str | None
    matched_routes: tuple[CorpusFacetRoute, ...]


class CorpusFacetRouter:
    """Resolve trusted structured facets into document-level epistemic routes.

    Only rules explicitly registered in corpus_facet_route participate. The
    router never examines document prose and never treats character/freeform
    tags as permissions by themselves.
    """

    ROUTABLE_FACET_TYPES = frozenset({"fandom"})

    def __init__(self, db: Database):
        self.db = db

    async def resolve(self, classification: CorpusClassification) -> CorpusDocumentRoute:
        facets = {
            (str(f.facet_type).strip().lower(), normalize_facet_value(f.facet_value))
            for f in classification.facets
            if str(f.facet_type).strip().lower() in self.ROUTABLE_FACET_TYPES
            and normalize_facet_value(f.facet_value)
        }
        if not facets:
            return CorpusDocumentRoute((), (), classification.epistemic_namespace, ())

        rows = await self.db.fetch(
            """SELECT facet_type, facet_value, knowledge_domain, scope_key,
                      epistemic_namespace, access_class, priority, meta
               FROM aios.corpus_facet_route
               WHERE enabled=TRUE
                 AND facet_type = ANY($1::text[])
               ORDER BY priority DESC, knowledge_domain, scope_key""",
            sorted({kind for kind, _ in facets}),
        )
        matched: list[CorpusFacetRoute] = []
        for row in rows:
            key = (str(row["facet_type"]).strip().lower(), normalize_facet_value(row["facet_value"]))
            if key not in facets:
                continue
            matched.append(CorpusFacetRoute(
                facet_type=key[0],
                facet_value=key[1],
                knowledge_domain=str(row["knowledge_domain"]),
                scope_key=str(row["scope_key"]),
                epistemic_namespace=str(row["epistemic_namespace"]),
                access_class=str(row["access_class"]),
                priority=int(row["priority"] or 0),
                meta=dict(row["meta"] or {}),
            ))

        scopes = tuple(dict.fromkeys(route.scope_key for route in matched))
        domains = tuple(dict.fromkeys(route.knowledge_domain for route in matched))
        namespaces = tuple(dict.fromkeys(route.epistemic_namespace for route in matched))
        if len(namespaces) == 1:
            namespace = namespaces[0]
        elif len(namespaces) > 1:
            namespace = "fanwork.crossover" if any(
                facet.facet_type == "canon_status" and normalize_facet_value(facet.facet_value) == "fanwork"
                for facet in classification.facets
            ) else "reference.crossover"
        else:
            namespace = classification.epistemic_namespace
        return CorpusDocumentRoute(scopes, domains, namespace, tuple(matched))

    async def apply(self, *, document_id, route: CorpusDocumentRoute) -> None:
        for matched in route.matched_routes:
            await self.db.execute(
                """INSERT INTO aios.corpus_scope (scope_key, display_name, access_class)
                   VALUES ($1,$1,$2)
                   ON CONFLICT (scope_key) DO UPDATE
                   SET access_class=CASE
                       WHEN aios.corpus_scope.access_class='restricted' THEN 'restricted'
                       ELSE EXCLUDED.access_class
                   END""",
                matched.scope_key, matched.access_class,
            )
            await self.db.execute(
                """INSERT INTO aios.corpus_document_scope (document_id, scope_key)
                   VALUES ($1,$2) ON CONFLICT DO NOTHING""",
                document_id, matched.scope_key,
            )
            await self.db.execute(
                """INSERT INTO aios.corpus_document_domain
                       (document_id, knowledge_domain, source, meta)
                   VALUES ($1,$2,'facet_route',$3::jsonb)
                   ON CONFLICT (document_id, knowledge_domain) DO UPDATE
                   SET meta=aios.corpus_document_domain.meta || EXCLUDED.meta""",
                document_id,
                matched.knowledge_domain,
                json.dumps({
                    "facet_type": matched.facet_type,
                    "facet_value": matched.facet_value,
                    "scope_key": matched.scope_key,
                    "epistemic_namespace": matched.epistemic_namespace,
                    "priority": matched.priority,
                    "route_meta": matched.meta,
                }),
            )
        if route.matched_routes:
            await self.db.execute(
                """DELETE FROM aios.corpus_document_scope
                   WHERE document_id=$1 AND scope_key='unclassified'""",
                document_id,
            )
            await self.db.execute(
                """UPDATE aios.corpus_document
                   SET epistemic_namespace=$2
                   WHERE document_id=$1""",
                document_id, route.epistemic_namespace or "reference",
            )


    async def reconcile_document(self, *, document_id) -> CorpusDocumentRoute:
        rows = await self.db.fetch(
            """SELECT facet_type, facet_value, source, confidence, meta
               FROM aios.corpus_document_facet
               WHERE document_id=$1
               ORDER BY facet_type, facet_value""",
            document_id,
        )
        doc = await self.db.fetchrow(
            "SELECT epistemic_namespace FROM aios.corpus_document WHERE document_id=$1",
            document_id,
        )
        if not doc:
            raise ValueError(f"unknown corpus document {document_id}")
        classification = CorpusClassification(
            facets=tuple(
                CorpusFacet(
                    facet_type=str(row["facet_type"]),
                    facet_value=str(row["facet_value"]),
                    source=str(row["source"] or "adapter"),
                    confidence=float(row["confidence"] or 1.0),
                    meta=dict(row["meta"] or {}),
                )
                for row in rows
            ),
            epistemic_namespace=str(doc["epistemic_namespace"] or "reference"),
        )
        route = await self.resolve(classification)

        # Recompute all facet-derived routing from the document's complete
        # current facet set. Manual/source-profile scopes are left untouched.
        await self.db.execute(
            """DELETE FROM aios.corpus_document_domain
               WHERE document_id=$1 AND source='facet_route'""",
            document_id,
        )
        await self.db.execute(
            """DELETE FROM aios.corpus_document_scope cds
               WHERE cds.document_id=$1
                 AND EXISTS (
                     SELECT 1 FROM aios.corpus_facet_route cfr
                     WHERE cfr.scope_key=cds.scope_key
                 )""",
            document_id,
        )
        await self.apply(document_id=document_id, route=route)
        return route

    async def reconcile_matching_documents(
        self, *, facet_type: str, facet_value: str
    ) -> int:
        rows = await self.db.fetch(
            """SELECT DISTINCT document_id
               FROM aios.corpus_document_facet
               WHERE lower(facet_type)=lower($1)
                 AND lower(regexp_replace(trim(facet_value), '\\s+', ' ', 'g'))=$2
               ORDER BY document_id""",
            facet_type,
            normalize_facet_value(facet_value),
        )
        for row in rows:
            await self.reconcile_document(document_id=row["document_id"])
        return len(rows)


async def ensure_facet_route(
    db: Database,
    *,
    facet_type: str,
    facet_value: str,
    knowledge_domain: str,
    scope_key: str,
    epistemic_namespace: str,
    access_class: str = "domain",
    priority: int = 0,
    meta: dict | None = None,
) -> dict:
    facet_type = facet_type.strip().lower()
    facet_value = normalize_facet_value(facet_value)
    if facet_type not in CorpusFacetRouter.ROUTABLE_FACET_TYPES:
        raise ValueError(f"facet type {facet_type!r} is not trusted for corpus routing")
    if access_class not in {"domain", "restricted"}:
        raise ValueError("facet routes may only create domain or restricted scopes")
    row = await db.execute_returning_row(
        """INSERT INTO aios.corpus_facet_route
               (facet_type, facet_value, knowledge_domain, scope_key,
                epistemic_namespace, access_class, priority, meta)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
           ON CONFLICT (facet_type, facet_value, knowledge_domain) DO UPDATE
           SET scope_key=EXCLUDED.scope_key,
               epistemic_namespace=EXCLUDED.epistemic_namespace,
               access_class=EXCLUDED.access_class,
               priority=EXCLUDED.priority,
               meta=aios.corpus_facet_route.meta || EXCLUDED.meta,
               enabled=TRUE,
               updated_at=now()
           RETURNING facet_type, facet_value, knowledge_domain, scope_key,
                     epistemic_namespace, access_class, priority, enabled, meta""",
        facet_type, facet_value, knowledge_domain, scope_key,
        epistemic_namespace, access_class, int(priority), json.dumps(meta or {}),
    )
    result = dict(row)
    result["documents_reclassified"] = await CorpusFacetRouter(
        db
    ).reconcile_matching_documents(
        facet_type=facet_type,
        facet_value=facet_value,
    )
    return result
