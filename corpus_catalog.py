from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.parse import urlparse
from uuid import UUID

from aios_app.db import Database


@dataclass(frozen=True)
class CorpusRoute:
    collection_key: str
    scope_key: str
    epistemic_namespace: str
    identity_binding: str
    profile_id: UUID | None = None
    profile_key: str | None = None
    matched_by: str = "fallback"

    @property
    def classified(self) -> bool:
        return self.profile_id is not None


def _host(uri: str | None) -> str:
    if not uri:
        return ""
    value = uri if "://" in uri else f"https://{uri}"
    return (urlparse(value).hostname or "").lower().rstrip(".")


def _path_matches(uri: str | None, prefix: str | None) -> bool:
    if not prefix:
        return True
    if not uri:
        return False
    path = urlparse(uri if "://" in uri else f"https://{uri}").path or "/"
    normalized = "/" + prefix.strip().lstrip("/")
    return path == normalized or path.startswith(normalized.rstrip("/") + "/")


def _domain_matches(host: str, pattern: str | None) -> bool:
    pattern = (pattern or "").lower().strip().rstrip(".")
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        pattern = pattern[1:]
    if pattern.startswith("."):
        return host.endswith(pattern) and host != pattern[1:]
    return host == pattern


class CorpusCatalogService:
    """Deterministic trusted-source routing for cold corpus material."""

    FALLBACK = CorpusRoute(
        collection_key="inbox",
        scope_key="unclassified",
        epistemic_namespace="reference",
        identity_binding="external",
    )

    def __init__(self, db: Database):
        self.db = db

    async def resolve(
        self,
        *,
        source_id: str | None,
        source_uri: str | None,
    ) -> CorpusRoute:
        host = _host(source_uri)
        rows = await self.db.fetch(
            """
            SELECT profile_id, profile_key, source_id, domain_pattern,
                   collection_key, scope_key, epistemic_namespace,
                   identity_binding, priority
            FROM aios.corpus_source_profile
            WHERE enabled=TRUE
              AND (source_id=$1 OR domain_pattern IS NOT NULL)
            ORDER BY priority DESC, created_at ASC
            """,
            source_id,
        )
        candidates: list[tuple[int, int, object, str]] = []
        for row in rows:
            exact_source = bool(source_id and row["source_id"] == source_id)
            domain = _domain_matches(host, row["domain_pattern"])
            if not exact_source and not domain:
                continue
            # Source identity is more specific than domain at equal priority.
            specificity = 2 if exact_source else 1
            candidates.append((int(row["priority"] or 0), specificity, row, "source_id" if exact_source else "domain"))
        if not candidates:
            return self.FALLBACK
        _, _, row, matched_by = max(candidates, key=lambda item: (item[0], item[1]))
        return CorpusRoute(
            collection_key=row["collection_key"],
            scope_key=row["scope_key"],
            epistemic_namespace=row["epistemic_namespace"],
            identity_binding=row["identity_binding"],
            profile_id=row["profile_id"],
            profile_key=row["profile_key"],
            matched_by=matched_by,
        )

    async def ensure_profile(
        self,
        *,
        profile_key: str,
        collection_key: str,
        scope_key: str,
        display_name: str | None = None,
        source_id: str | None = None,
        domain_pattern: str | None = None,
        epistemic_namespace: str = "reference",
        identity_binding: str = "external",
        priority: int = 0,
        meta: dict | None = None,
    ) -> dict:
        if not source_id and not domain_pattern:
            raise ValueError("source profile requires source_id or domain_pattern")
        if identity_binding not in {"external", "world", "character"}:
            raise ValueError(f"unsupported corpus identity_binding {identity_binding!r}")
        profile_key = profile_key.strip()
        collection_key = collection_key.strip()
        scope_key = scope_key.strip()
        if not profile_key or not collection_key or not scope_key:
            raise ValueError("profile_key, collection_key and scope_key are required")
        domain_pattern = (domain_pattern or "").lower().strip().rstrip(".") or None\n        path_prefix = (path_prefix or "").strip() or None\n        knowledge_domain = (knowledge_domain or "").strip() or None

        await self.db.execute(
            """
            INSERT INTO aios.corpus_collection (collection_key, display_name, meta)
            VALUES ($1,$2,$3::jsonb)
            ON CONFLICT (collection_key) DO UPDATE
            SET display_name=COALESCE(EXCLUDED.display_name, aios.corpus_collection.display_name),
                meta=aios.corpus_collection.meta || EXCLUDED.meta,
                updated_at=now()
            """,
            collection_key, display_name, json.dumps(meta or {}),
        )
        await self.db.execute(
            """
            INSERT INTO aios.corpus_scope (scope_key, display_name)
            VALUES ($1,$2)
            ON CONFLICT (scope_key) DO NOTHING
            """,
            scope_key, display_name or scope_key,
        )
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.corpus_source_profile (
                profile_key, source_id, domain_pattern, collection_key, scope_key,
                epistemic_namespace, identity_binding, priority, meta
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
            ON CONFLICT (profile_key) DO UPDATE
            SET source_id=EXCLUDED.source_id,
                domain_pattern=EXCLUDED.domain_pattern,
                collection_key=EXCLUDED.collection_key,
                scope_key=EXCLUDED.scope_key,
                epistemic_namespace=EXCLUDED.epistemic_namespace,
                identity_binding=EXCLUDED.identity_binding,
                priority=EXCLUDED.priority,
                meta=aios.corpus_source_profile.meta || EXCLUDED.meta,
                enabled=TRUE,
                updated_at=now()
            RETURNING profile_id, profile_key, collection_key, scope_key,
                      epistemic_namespace, identity_binding
            """,
            profile_key, source_id, domain_pattern, collection_key, scope_key,
            epistemic_namespace, identity_binding, int(priority), json.dumps(meta or {}),
        )
        return dict(row)

    async def assign_document(self, *, document_id: UUID, route: CorpusRoute) -> None:
        await self.db.execute(
            """
            INSERT INTO aios.corpus_document_collection (
                document_id, collection_key, assigned_by, profile_id, meta
            )
            VALUES ($1,$2,$3,$4,$5::jsonb)
            ON CONFLICT (document_id, collection_key) DO UPDATE
            SET assigned_by=EXCLUDED.assigned_by,
                profile_id=EXCLUDED.profile_id,
                meta=aios.corpus_document_collection.meta || EXCLUDED.meta
            """,
            document_id,
            route.collection_key,
            "source_profile" if route.classified else "catalog_fallback",
            route.profile_id,
            json.dumps({"profile_key": route.profile_key, "matched_by": route.matched_by}),
        )

    async def reclassify_profile(self, profile_key: str) -> dict:
        profile = await self.db.fetchrow(
            """
            SELECT profile_id, profile_key, source_id, domain_pattern, collection_key,
                   scope_key, epistemic_namespace, identity_binding
            FROM aios.corpus_source_profile
            WHERE profile_key=$1 AND enabled=TRUE
            """,
            profile_key,
        )
        if not profile:
            raise ValueError(f"unknown enabled corpus source profile {profile_key!r}")

        rows = await self.db.fetch(
            """
            SELECT document_id, source_id, source_uri
            FROM aios.corpus_document
            WHERE ($1::text IS NULL OR source_id=$1)
            """,
            profile["source_id"],
        )
        matched = []
        for row in rows:
            if profile["source_id"] and row["source_id"] != profile["source_id"]:
                continue
            if profile["domain_pattern"] and not _domain_matches(_host(row["source_uri"]), profile["domain_pattern"]):
                continue
            matched.append(row["document_id"])

        route = CorpusRoute(
            collection_key=profile["collection_key"],
            scope_key=profile["scope_key"],
            epistemic_namespace=profile["epistemic_namespace"],
            identity_binding=profile["identity_binding"],
            profile_id=profile["profile_id"],
            profile_key=profile["profile_key"],
            matched_by="reclassification",
        )
        for document_id in matched:
            await self.db.execute(
                "DELETE FROM aios.corpus_document_scope WHERE document_id=$1 AND scope_key='unclassified'",
                document_id,
            )
            await self.db.execute(
                """
                INSERT INTO aios.corpus_document_scope (document_id, scope_key)
                VALUES ($1,$2) ON CONFLICT DO NOTHING
                """,
                document_id, route.scope_key,
            )
            await self.db.execute(
                """
                UPDATE aios.corpus_document
                SET epistemic_namespace=$2, identity_binding=$3
                WHERE document_id=$1
                """,
                document_id, route.epistemic_namespace, route.identity_binding,
            )
            await self.db.execute(
                """
                DELETE FROM aios.corpus_document_collection
                WHERE document_id=$1 AND collection_key='inbox'
                """,
                document_id,
            )
            await self.assign_document(document_id=document_id, route=route)
        return {"profile_key": profile_key, "reclassified_documents": len(matched)}


class CorpusAccessReconciler:
    """Materialize hard corpus ACLs from authored character/domain eligibility."""

    def __init__(self, db: Database):
        self.db = db

    async def reconcile_character(self, character_id: str) -> dict:
        domains = await self.db.fetch(
            """SELECT knowledge_domain FROM aios.character_knowledge_domain
               WHERE character_id=$1 AND enabled=TRUE""",
            character_id,
        )
        domain_keys = [row["knowledge_domain"] for row in domains]
        if not domain_keys:
            return {"character_id": character_id, "knowledge_domains": [], "granted_scopes": []}
        rows = await self.db.fetch(
            """SELECT DISTINCT scope_key
               FROM aios.corpus_source_profile
               WHERE enabled=TRUE AND knowledge_domain = ANY($1::text[])""",
            domain_keys,
        )
        scopes = sorted({row["scope_key"] for row in rows})
        for scope_key in scopes:
            await self.db.execute(
                """INSERT INTO aios.character_corpus_access
                       (character_id, scope_key, allowed, meta)
                   VALUES ($1,$2,TRUE,$3::jsonb)
                   ON CONFLICT (character_id, scope_key) DO UPDATE
                   SET allowed=TRUE,
                       meta=aios.character_corpus_access.meta || EXCLUDED.meta,
                       updated_at=now()""",
                character_id, scope_key,
                json.dumps({"derived_from": "knowledge_domain", "knowledge_domains": domain_keys}),
            )
        return {"character_id": character_id, "knowledge_domains": domain_keys, "granted_scopes": scopes}

    async def reconcile_domain(self, knowledge_domain: str) -> dict:
        rows = await self.db.fetch(
            """SELECT character_id FROM aios.character_knowledge_domain
               WHERE knowledge_domain=$1 AND enabled=TRUE""",
            knowledge_domain,
        )
        results = []
        for row in rows:
            results.append(await self.reconcile_character(row["character_id"]))
        return {"knowledge_domain": knowledge_domain, "characters": results}

    async def set_character_domains(self, character_id: str, domains: list[str]) -> dict:
        normalized = sorted({str(value).strip() for value in domains if str(value).strip()})
        await self.db.execute(
            """UPDATE aios.character_knowledge_domain
               SET enabled=FALSE, updated_at=now()
               WHERE character_id=$1""",
            character_id,
        )
        for domain in normalized:
            await self.db.execute(
                """INSERT INTO aios.character_knowledge_domain
                       (character_id, knowledge_domain, enabled, meta)
                   VALUES ($1,$2,TRUE,$3::jsonb)
                   ON CONFLICT (character_id, knowledge_domain) DO UPDATE
                   SET enabled=TRUE, updated_at=now()""",
                character_id, domain, json.dumps({"assigned_by": "character_configuration"}),
            )
        return await self.reconcile_character(character_id)
