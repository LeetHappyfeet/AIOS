from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database


@dataclass(frozen=True)
class InferenceProvider:
    provider_id: UUID
    provider_key: str
    display_name: str
    base_url: str
    model: str
    api_key_env: str | None
    api_key_secret: str | None
    provider_type: str
    enabled: bool
    drain: bool
    max_concurrency: int
    context_window: int | None
    timeout_seconds: float
    worker_classes: tuple[str, ...]
    strict_worker_classes: bool
    capabilities: dict[str, Any]
    status: str
    consecutive_failures: int
    protocol_failures: int
    cooldown_until: Any
    last_error: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "InferenceProvider":
        caps = row["capabilities"]
        if isinstance(caps, str):
            caps = json.loads(caps)
        return cls(
            provider_id=row["provider_id"], provider_key=row["provider_key"],
            display_name=row["display_name"], base_url=row["base_url"].rstrip("/"),
            model=row["model"], api_key_env=row["api_key_env"],
            api_key_secret=row.get("api_key_secret"), provider_type=str(row.get("provider_type") or "openai_compatible"),
            enabled=bool(row["enabled"]), drain=bool(row["drain"]),
            max_concurrency=int(row["max_concurrency"]), context_window=row["context_window"],
            timeout_seconds=float(row["timeout_seconds"]),
            worker_classes=tuple(row["worker_classes"] or ()),
            strict_worker_classes=bool(row.get("strict_worker_classes", False)),
            capabilities=dict(caps or {}), status=row["status"],
            consecutive_failures=int(row["consecutive_failures"] or 0),
            protocol_failures=int(row.get("protocol_failures", 0) or 0),
            cooldown_until=row["cooldown_until"], last_error=row["last_error"],
        )


class InferenceProviderStore:
    def __init__(self, db: Database):
        self.db = db

    async def upsert(
        self, *, provider_key: str, display_name: str, base_url: str, model: str,
        api_key_env: str | None = None, api_key_secret: str | None = None,
        provider_type: str = "openai_compatible", enabled: bool = True, drain: bool = False,
        max_concurrency: int = 1, context_window: int | None = None,
        timeout_seconds: float = 900.0, worker_classes: list[str] | None = None,
        strict_worker_classes: bool = False,
        capabilities: Mapping[str, Any] | None = None,
    ) -> InferenceProvider:
        normalized_url = base_url.rstrip("/")
        duplicate = await self.db.fetchrow(
            """SELECT provider_id, provider_key FROM aios.inference_provider
               WHERE lower(rtrim(base_url,'/'))=lower($1) AND model=$2 AND provider_key<>$3
               LIMIT 1""", normalized_url, model, provider_key,
        )
        if duplicate:
            raise ValueError(
                f"Endpoint/model is already registered as '{duplicate['provider_key']}'. "
                "Update that worker or remove it before adding a duplicate."
            )
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.inference_provider (
                provider_key, display_name, base_url, model, api_key_env, api_key_secret,
                provider_type, enabled, drain, max_concurrency, context_window,
                timeout_seconds, worker_classes, strict_worker_classes, capabilities, status
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::text[],$14,$15::jsonb,
                      CASE WHEN $8 THEN 'unknown' ELSE 'disabled' END)
            ON CONFLICT (provider_key) DO UPDATE SET
                display_name=EXCLUDED.display_name, base_url=EXCLUDED.base_url,
                model=EXCLUDED.model, api_key_env=EXCLUDED.api_key_env,
                api_key_secret=CASE WHEN EXCLUDED.api_key_secret IS NULL OR EXCLUDED.api_key_secret=''
                                    THEN inference_provider.api_key_secret ELSE EXCLUDED.api_key_secret END,
                provider_type=EXCLUDED.provider_type, enabled=EXCLUDED.enabled, drain=EXCLUDED.drain,
                max_concurrency=EXCLUDED.max_concurrency, context_window=EXCLUDED.context_window,
                timeout_seconds=EXCLUDED.timeout_seconds, worker_classes=EXCLUDED.worker_classes,
                strict_worker_classes=EXCLUDED.strict_worker_classes,
                capabilities=EXCLUDED.capabilities,
                status=CASE WHEN EXCLUDED.enabled THEN inference_provider.status ELSE 'disabled' END,
                updated_at=now()
            RETURNING *
            """,
            provider_key, display_name, normalized_url, model, api_key_env, api_key_secret,
            provider_type, enabled, drain, max(1, int(max_concurrency)), context_window,
            float(timeout_seconds), list(worker_classes or []), strict_worker_classes,
            json.dumps(dict(capabilities or {})),
        )
        return InferenceProvider.from_row(row)

    async def list(self) -> list[InferenceProvider]:
        rows = await self.db.fetch("SELECT * FROM aios.inference_provider ORDER BY enabled DESC, provider_key")
        return [InferenceProvider.from_row(r) for r in rows]

    async def get(self, provider_id: UUID) -> InferenceProvider | None:
        row = await self.db.fetchrow("SELECT * FROM aios.inference_provider WHERE provider_id=$1", provider_id)
        return InferenceProvider.from_row(row) if row else None

    async def delete(self, provider_id: UUID, *, force: bool = False) -> None:
        active = await self.db.fetchrow(
            "SELECT count(*) AS n FROM aios.inference_request WHERE provider_id=$1 AND status='running'",
            provider_id,
        )
        if active and int(active["n"]) and not force:
            raise RuntimeError("Worker has active inference requests; drain it first or force removal.")
        result = await self.db.execute("DELETE FROM aios.inference_provider WHERE provider_id=$1", provider_id)
        if result.endswith("0"):
            raise LookupError(f"Unknown inference provider {provider_id}")

    async def set_control(self, provider_id: UUID, *, enabled: bool | None = None, drain: bool | None = None) -> InferenceProvider:
        row = await self.db.execute_returning_row(
            """UPDATE aios.inference_provider SET
                 enabled=COALESCE($2, enabled), drain=COALESCE($3, drain),
                 status=CASE WHEN COALESCE($2, enabled)=false THEN 'disabled'
                             WHEN status='disabled' THEN 'unknown' ELSE status END,
                 updated_at=now()
               WHERE provider_id=$1 RETURNING *""", provider_id, enabled, drain,
        )
        if not row:
            raise LookupError(f"Unknown inference provider {provider_id}")
        return InferenceProvider.from_row(row)

    async def routable(self, worker_class: str) -> list[InferenceProvider]:
        rows = await self.db.fetch(
            """
            SELECT p.*
            FROM aios.inference_provider p
            WHERE p.enabled=true AND p.drain=false
              AND p.status IN ('ready','unknown','degraded')
              AND (NOT p.strict_worker_classes OR cardinality(p.worker_classes)=0 OR $1=ANY(p.worker_classes))
              AND (p.cooldown_until IS NULL OR p.cooldown_until <= now())
              AND (SELECT count(*) FROM aios.inference_request r
                   WHERE r.provider_id=p.provider_id AND r.status='running'
                     AND (r.lease_expires_at IS NULL OR r.lease_expires_at > now())) < p.max_concurrency
            ORDER BY
              CASE WHEN $1=ANY(p.worker_classes) THEN 0 WHEN cardinality(p.worker_classes)=0 THEN 1 ELSE 2 END,
              CASE p.status WHEN 'ready' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
              ((SELECT count(*)::float FROM aios.inference_request r
                WHERE r.provider_id=p.provider_id AND r.status='running'
                  AND (r.lease_expires_at IS NULL OR r.lease_expires_at > now())) / p.max_concurrency),
              p.consecutive_failures, p.last_success_at DESC NULLS LAST, p.provider_key
            """, worker_class,
        )
        return [InferenceProvider.from_row(r) for r in rows]

    async def due_for_health(self, limit: int = 100) -> list[InferenceProvider]:
        rows = await self.db.fetch(
            """SELECT * FROM aios.inference_provider
               WHERE enabled=true AND (
                 last_health_at IS NULL OR
                 last_health_at + (
                   CASE WHEN status='offline' THEN offline_health_interval_seconds
                        ELSE health_interval_seconds END * interval '1 second'
                 ) <= now()
               )
               ORDER BY last_health_at NULLS FIRST LIMIT $1""", max(1, min(int(limit), 1000)),
        )
        return [InferenceProvider.from_row(r) for r in rows]

    async def reap_stale_requests(self) -> int:
        result = await self.db.execute(
            """UPDATE aios.inference_request SET status='failed',
                 error=COALESCE(error,'inference lease expired; worker/process disappeared'),
                 completed_at=now(), updated_at=now()
               WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at < now()"""
        )
        try:
            return int(result.rsplit(" ", 1)[-1])
        except Exception:
            return 0

    async def record_protocol_failure(self, provider_id: UUID, error: str) -> None:
        await self.db.execute(
            """UPDATE aios.inference_provider SET protocol_failures=protocol_failures+1,
                 last_error=$2, updated_at=now() WHERE provider_id=$1""",
            provider_id, error[:1000],
        )

    async def record_health(self, provider_id: UUID, *, ok: bool, error: str | None = None, latency_ms: int | None = None) -> None:
        await self.db.execute(
            """UPDATE aios.inference_provider SET
                 status=CASE WHEN enabled=false THEN 'disabled' WHEN $2 THEN 'ready'
                             WHEN consecutive_failures + 1 >= 3 THEN 'offline' ELSE 'degraded' END,
                 consecutive_failures=CASE WHEN $2 THEN 0 ELSE consecutive_failures + 1 END,
                 last_health_at=now(), last_health_latency_ms=COALESCE($4,last_health_latency_ms),
                 last_success_at=CASE WHEN $2 THEN now() ELSE last_success_at END,
                 last_failure_at=CASE WHEN $2 THEN last_failure_at ELSE now() END,
                 cooldown_until=CASE WHEN NOT $2 AND consecutive_failures + 1 >= 3
                                     THEN now() + interval '60 seconds' ELSE NULL END,
                 last_error=CASE WHEN $2 THEN NULL ELSE $3 END, updated_at=now()
               WHERE provider_id=$1""", provider_id, ok, error, latency_ms,
        )
