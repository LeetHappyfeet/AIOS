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
    enabled: bool
    drain: bool
    max_concurrency: int
    context_window: int | None
    timeout_seconds: float
    worker_classes: tuple[str, ...]
    capabilities: dict[str, Any]
    status: str
    consecutive_failures: int
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
            enabled=bool(row["enabled"]), drain=bool(row["drain"]),
            max_concurrency=int(row["max_concurrency"]),
            context_window=row["context_window"],
            timeout_seconds=float(row["timeout_seconds"]),
            worker_classes=tuple(row["worker_classes"] or ()),
            capabilities=dict(caps or {}), status=row["status"],
            consecutive_failures=int(row["consecutive_failures"] or 0),
            cooldown_until=row["cooldown_until"], last_error=row["last_error"],
        )


class InferenceProviderStore:
    def __init__(self, db: Database):
        self.db = db

    async def upsert(
        self, *, provider_key: str, display_name: str, base_url: str, model: str,
        api_key_env: str | None = None, enabled: bool = True, drain: bool = False,
        max_concurrency: int = 1, context_window: int | None = None,
        timeout_seconds: float = 90.0, worker_classes: list[str] | None = None,
        capabilities: Mapping[str, Any] | None = None,
    ) -> InferenceProvider:
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.inference_provider (
                provider_key, display_name, base_url, model, api_key_env, enabled,
                drain, max_concurrency, context_window, timeout_seconds,
                worker_classes, capabilities, status
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::text[],$12::jsonb,
                      CASE WHEN $6 THEN 'unknown' ELSE 'disabled' END)
            ON CONFLICT (provider_key) DO UPDATE SET
                display_name=EXCLUDED.display_name, base_url=EXCLUDED.base_url,
                model=EXCLUDED.model, api_key_env=EXCLUDED.api_key_env,
                enabled=EXCLUDED.enabled, drain=EXCLUDED.drain,
                max_concurrency=EXCLUDED.max_concurrency,
                context_window=EXCLUDED.context_window,
                timeout_seconds=EXCLUDED.timeout_seconds,
                worker_classes=EXCLUDED.worker_classes,
                capabilities=EXCLUDED.capabilities,
                status=CASE WHEN EXCLUDED.enabled THEN inference_provider.status ELSE 'disabled' END,
                updated_at=now()
            RETURNING *
            """,
            provider_key, display_name, base_url.rstrip("/"), model, api_key_env,
            enabled, drain, max(1, int(max_concurrency)), context_window,
            float(timeout_seconds), list(worker_classes or []),
            json.dumps(dict(capabilities or {})),
        )
        return InferenceProvider.from_row(row)

    async def list(self) -> list[InferenceProvider]:
        rows = await self.db.fetch(
            "SELECT * FROM aios.inference_provider ORDER BY enabled DESC, provider_key"
        )
        return [InferenceProvider.from_row(r) for r in rows]

    async def get(self, provider_id: UUID) -> InferenceProvider | None:
        row = await self.db.fetchrow(
            "SELECT * FROM aios.inference_provider WHERE provider_id=$1", provider_id
        )
        return InferenceProvider.from_row(row) if row else None

    async def set_control(
        self, provider_id: UUID, *, enabled: bool | None = None, drain: bool | None = None
    ) -> InferenceProvider:
        row = await self.db.execute_returning_row(
            """
            UPDATE aios.inference_provider SET
                enabled=COALESCE($2, enabled),
                drain=COALESCE($3, drain),
                status=CASE
                    WHEN COALESCE($2, enabled)=false THEN 'disabled'
                    WHEN status='disabled' THEN 'unknown'
                    ELSE status
                END,
                updated_at=now()
            WHERE provider_id=$1 RETURNING *
            """,
            provider_id, enabled, drain,
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
              AND (cardinality(p.worker_classes)=0 OR $1=ANY(p.worker_classes))
              AND (p.cooldown_until IS NULL OR p.cooldown_until <= now())
              AND (
                  SELECT count(*) FROM aios.inference_request r
                  WHERE r.provider_id=p.provider_id AND r.status='running'
              ) < p.max_concurrency
            ORDER BY
              CASE p.status WHEN 'ready' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
              p.consecutive_failures ASC,
              p.last_success_at DESC NULLS LAST,
              p.provider_key
            """,
            worker_class,
        )
        return [InferenceProvider.from_row(r) for r in rows]

    async def record_health(
        self, provider_id: UUID, *, ok: bool, error: str | None = None
    ) -> None:
        await self.db.execute(
            """
            UPDATE aios.inference_provider SET
                status=CASE
                    WHEN enabled=false THEN 'disabled'
                    WHEN $2 THEN 'ready'
                    WHEN consecutive_failures + 1 >= 3 THEN 'offline'
                    ELSE 'degraded'
                END,
                consecutive_failures=CASE WHEN $2 THEN 0 ELSE consecutive_failures + 1 END,
                last_health_at=now(),
                last_success_at=CASE WHEN $2 THEN now() ELSE last_success_at END,
                last_failure_at=CASE WHEN $2 THEN last_failure_at ELSE now() END,
                cooldown_until=CASE
                    WHEN NOT $2 AND consecutive_failures + 1 >= 3 THEN now() + interval '60 seconds'
                    ELSE NULL
                END,
                last_error=CASE WHEN $2 THEN NULL ELSE $3 END,
                updated_at=now()
            WHERE provider_id=$1
            """,
            provider_id, ok, error,
        )
