from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable

from aios_app.corpus_adapters import CorpusClassification, CorpusFacet
from aios_app.db import Database


_WS_RE = re.compile(r"\s+")


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    try:
        return dict(value or {})
    except (TypeError, ValueError):
        return {}


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

    ROUTABLE_FACET_TYPES = frozenset({"fandom", "franchise", "universe", "subject", "domain"})

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
            """SELECT kdi.identifier_type AS facet_type,
                      kdi.identifier_value AS facet_value,
                      kd.domain_key AS knowledge_domain,
                      COALESCE(kd.default_scope_key, kd.domain_key) AS scope_key,
                      kd.default_epistemic_namespace AS epistemic_namespace,
                      COALESCE(cs.access_class, 'domain') AS access_class,
                      COALESCE((kdi.meta->>'priority')::integer, 0) AS priority,
                      kdi.meta
               FROM aios.knowledge_domain_identifier kdi
               JOIN aios.knowledge_domain kd
                 ON kd.domain_id=kdi.domain_id AND kd.enabled
               LEFT JOIN aios.corpus_scope cs
                 ON cs.scope_key=COALESCE(kd.default_scope_key, kd.domain_key)
               WHERE kdi.identifier_type = ANY($1::text[])
               ORDER BY priority DESC, kd.domain_key""",
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
                meta=_json_object(row["meta"]),
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

    async def observe_unresolved(self, *, document_id, classification: CorpusClassification, route: CorpusDocumentRoute) -> int:
        """Inventory unknown trusted structured identifiers without granting access."""
        matched = {
            (r.facet_type, normalize_facet_value(r.facet_value))
            for r in route.matched_routes
        }
        unresolved = {
            (str(f.facet_type).strip().lower(), normalize_facet_value(f.facet_value))
            for f in classification.facets
            if str(f.facet_type).strip().lower() in self.ROUTABLE_FACET_TYPES
            and normalize_facet_value(f.facet_value)
            and (str(f.facet_type).strip().lower(), normalize_facet_value(f.facet_value)) not in matched
        }
        for identifier_type, identifier_value in unresolved:
            await self.db.execute(
                """INSERT INTO aios.knowledge_domain_candidate (
                       identifier_type, identifier_value, occurrence_count,
                       first_document_id, last_document_id, source, meta
                   )
                   VALUES ($1,$2,1,$3,$3,'corpus_structured_metadata',
                           jsonb_build_object('evidence','trusted_structured_facet'))
                   ON CONFLICT (identifier_type, identifier_value) DO UPDATE
                   SET occurrence_count=aios.knowledge_domain_candidate.occurrence_count+1,
                       last_document_id=EXCLUDED.last_document_id,
                       last_seen_at=now()""",
                identifier_type, identifier_value, document_id,
            )
        return len(unresolved)

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
                    meta=_json_object(row["meta"]),
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
        await self.observe_unresolved(
            document_id=document_id,
            classification=classification,
            route=route,
        )
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


async def ensure_knowledge_domain(
    db: Database,
    *,
    domain_key: str,
    display_name: str,
    domain_kind: str = "general",
    parent_domain_key: str | None = None,
    default_scope_key: str | None = None,
    default_epistemic_namespace: str = "reference",
    meta: dict | None = None,
) -> dict:
    parent_id = None
    if parent_domain_key:
        parent = await db.fetchrow(
            "SELECT domain_id FROM aios.knowledge_domain WHERE domain_key=$1 AND enabled",
            parent_domain_key.strip(),
        )
        if not parent:
            raise ValueError(f"unknown parent knowledge domain {parent_domain_key!r}")
        parent_id = parent["domain_id"]
    row = await db.execute_returning_row(
        """INSERT INTO aios.knowledge_domain (
               domain_key, display_name, domain_kind, parent_domain_id,
               default_scope_key, default_epistemic_namespace, meta
           )
           VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
           ON CONFLICT (domain_key) DO UPDATE
           SET display_name=EXCLUDED.display_name,
               domain_kind=EXCLUDED.domain_kind,
               parent_domain_id=EXCLUDED.parent_domain_id,
               default_scope_key=COALESCE(EXCLUDED.default_scope_key, aios.knowledge_domain.default_scope_key),
               default_epistemic_namespace=EXCLUDED.default_epistemic_namespace,
               meta=aios.knowledge_domain.meta || EXCLUDED.meta,
               enabled=TRUE,
               updated_at=now()
           RETURNING domain_id, domain_key, display_name, domain_kind,
                     parent_domain_id, default_scope_key,
                     default_epistemic_namespace, enabled, meta""",
        domain_key.strip(), display_name.strip(), domain_kind.strip(), parent_id,
        default_scope_key.strip() if default_scope_key else None,
        default_epistemic_namespace.strip(), json.dumps(meta or {}),
    )
    return dict(row)


async def register_domain_identifier(
    db: Database,
    *,
    domain_key: str,
    identifier_type: str,
    identifier_value: str,
    source: str = "operator",
    confidence: float = 1.0,
    meta: dict | None = None,
) -> dict:
    identifier_type = identifier_type.strip().lower()
    identifier_value = normalize_facet_value(identifier_value)
    if identifier_type not in CorpusFacetRouter.ROUTABLE_FACET_TYPES:
        raise ValueError(f"identifier type {identifier_type!r} is not trusted for corpus routing")
    domain = await db.fetchrow(
        """SELECT domain_id, default_scope_key, default_epistemic_namespace
           FROM aios.knowledge_domain WHERE domain_key=$1 AND enabled""",
        domain_key.strip(),
    )
    if not domain:
        raise ValueError(f"unknown knowledge domain {domain_key!r}")
    row = await db.execute_returning_row(
        """INSERT INTO aios.knowledge_domain_identifier (
               identifier_type, identifier_value, domain_id, source, confidence, meta
           )
           VALUES ($1,$2,$3,$4,$5,$6::jsonb)
           ON CONFLICT (identifier_type, identifier_value, domain_id) DO UPDATE
           SET source=EXCLUDED.source,
               confidence=EXCLUDED.confidence,
               meta=aios.knowledge_domain_identifier.meta || EXCLUDED.meta,
               updated_at=now()
           RETURNING identifier_type, identifier_value, domain_id, source,
                     confidence, meta""",
        identifier_type, identifier_value, domain["domain_id"], source,
        float(confidence), json.dumps(meta or {}),
    )
    await db.execute(
        """UPDATE aios.knowledge_domain_candidate
           SET status='resolved', resolved_domain_id=$3, resolved_at=now(),
               last_seen_at=now()
           WHERE identifier_type=$1 AND identifier_value=$2""",
        identifier_type, identifier_value, domain["domain_id"],
    )
    reconciled = await CorpusFacetRouter(db).reconcile_matching_documents(
        facet_type=identifier_type, facet_value=identifier_value
    )
    # Character sources that previously exposed this structured identifier
    # become visibly resolvable too. Their identity is not silently rewritten;
    # re-bootstrap/acceptance remains the authority boundary.
    from aios_app.char.domain_resolution import reconcile_character_domain_candidates
    character_candidates = await reconcile_character_domain_candidates(
        db,
        identifier_type=identifier_type,
        identifier_value=identifier_value,
    )

    result = dict(row)
    result["domain_key"] = domain_key.strip()
    result["documents_reclassified"] = reconciled
    result["character_candidates_now_resolvable"] = character_candidates
    return result


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
    """Compatibility API: author the canonical domain + identifier and mirror the legacy route."""
    facet_type = facet_type.strip().lower()
    facet_value = normalize_facet_value(facet_value)
    if facet_type not in CorpusFacetRouter.ROUTABLE_FACET_TYPES:
        raise ValueError(f"facet type {facet_type!r} is not trusted for corpus routing")
    if access_class not in {"domain", "restricted"}:
        raise ValueError("facet routes may only create domain or restricted scopes")

    await db.execute(
        """INSERT INTO aios.corpus_scope (scope_key, display_name, access_class)
           VALUES ($1,$1,$2)
           ON CONFLICT (scope_key) DO NOTHING""",
        scope_key, access_class,
    )
    await ensure_knowledge_domain(
        db,
        domain_key=knowledge_domain,
        display_name=knowledge_domain,
        domain_kind="fictional_universe" if knowledge_domain.startswith("fiction.") else "general",
        default_scope_key=scope_key,
        default_epistemic_namespace=epistemic_namespace,
        meta={"created_via": "facet_route_compatibility"},
    )
    identifier = await register_domain_identifier(
        db,
        domain_key=knowledge_domain,
        identifier_type=facet_type,
        identifier_value=facet_value,
        source="facet_route_compatibility",
        confidence=1.0,
        meta={"priority": int(priority), **(meta or {})},
    )
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
    result["documents_reclassified"] = identifier["documents_reclassified"]
    return result
