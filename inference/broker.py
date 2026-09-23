from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from .providers import InferenceProvider, InferenceProviderStore
from .protocol import (
    StructuredInferenceResponse,
    StructuredResponseError,
    extract_json_object,
    validate_structured_response,
)


class InferenceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class InferenceRequest:
    instance_id: UUID
    worker_class: str
    prompt: str
    task_id: UUID | None = None
    context_state_version: int | None = None
    hud_profile_name: str | None = None
    allowed_actions: Mapping[str, Mapping[str, Any]] | None = None
    output_schema: Mapping[str, Any] | None = None
    temperature: float = 0.2
    max_tokens: int = 1200


@dataclass(frozen=True)
class InferenceResult:
    request_id: UUID
    provider_id: UUID
    provider_key: str
    model: str
    response: StructuredInferenceResponse
    latency_ms: int


class OpenAICompatibleClient:
    async def health(self, provider: InferenceProvider) -> None:
        await asyncio.to_thread(self._models, provider)

    async def complete(self, provider: InferenceProvider, request: InferenceRequest) -> str:
        return await asyncio.to_thread(self._complete, provider, request)

    def _headers(self, provider: InferenceProvider) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if provider.api_key_env:
            token = os.getenv(provider.api_key_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _models(self, provider: InferenceProvider) -> None:
        req = urllib.request.Request(
            provider.base_url + "/models", headers=self._headers(provider), method="GET"
        )
        with urllib.request.urlopen(req, timeout=min(provider.timeout_seconds, 10.0)) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"health returned HTTP {response.status}")

    def _complete(self, provider: InferenceProvider, request: InferenceRequest) -> str:
        instruction = (
            "Return exactly one JSON object. Do not use markdown. "
            "Shape: {\"expression\": string, \"actions\": "
            "[{\"type\": string, \"arguments\": object}]}. "
            "Only propose action types explicitly listed in the prompt."
        )
        body = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if provider.capabilities.get("json_mode", True):
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            provider.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(provider),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=provider.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("provider returned no choices")
        content = ((choices[0].get("message") or {}).get("content"))
        if not isinstance(content, str):
            raise RuntimeError("provider returned no message content")
        return content


class InferenceBroker:
    def __init__(self, db: Database, client: OpenAICompatibleClient | None = None):
        self.db = db
        self.providers = InferenceProviderStore(db)
        self.client = client or OpenAICompatibleClient()

    async def health_check(self, provider_id: UUID) -> bool:
        provider = await self.providers.get(provider_id)
        if not provider:
            raise LookupError(f"Unknown inference provider {provider_id}")
        try:
            await self.client.health(provider)
        except Exception as exc:
            await self.providers.record_health(provider_id, ok=False, error=str(exc)[:1000])
            return False
        await self.providers.record_health(provider_id, ok=True)
        return True

    async def infer(self, request: InferenceRequest) -> InferenceResult:
        providers = await self.providers.routable(request.worker_class)
        if not providers:
            raise InferenceUnavailable(
                f"No donated inference provider available for '{request.worker_class}'"
            )
        last_error: Exception | None = None
        for provider in providers:
            try:
                return await self._attempt(provider, request)
            except Exception as exc:
                last_error = exc
                await self.providers.record_health(
                    provider.provider_id, ok=False, error=str(exc)[:1000]
                )
        raise InferenceUnavailable(
            f"All inference providers failed for '{request.worker_class}': {last_error}"
        )

    async def _attempt(
        self, provider: InferenceProvider, request: InferenceRequest
    ) -> InferenceResult:
        prompt_hash = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.inference_request (
                instance_id, task_id, provider_id, worker_class, model,
                context_state_version, hud_profile_name, prompt_hash,
                allowed_actions, output_schema, status, attempts, started_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,'running',1,now())
            RETURNING request_id
            """,
            request.instance_id, request.task_id, provider.provider_id,
            request.worker_class, provider.model, request.context_state_version,
            request.hud_profile_name, prompt_hash,
            json.dumps(dict(request.allowed_actions or {})),
            json.dumps(dict(request.output_schema)) if request.output_schema else None,
        )
        request_id = row["request_id"]
        started = time.monotonic()
        try:
            text = await self.client.complete(provider, request)
            payload = extract_json_object(text)
            structured = validate_structured_response(
                payload, allowed_actions=request.allowed_actions
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            await self.db.execute(
                """
                UPDATE aios.inference_request SET
                    status='succeeded', response_text=$2, response_json=$3::jsonb,
                    latency_ms=$4, completed_at=now(), updated_at=now()
                WHERE request_id=$1
                """,
                request_id, text, json.dumps(structured.raw), latency_ms,
            )
            await self.providers.record_health(provider.provider_id, ok=True)
            return InferenceResult(
                request_id, provider.provider_id, provider.provider_key,
                provider.model, structured, latency_ms
            )
        except StructuredResponseError as exc:
            await self.db.execute(
                """
                UPDATE aios.inference_request SET status='invalid',
                    validation_error=$2, completed_at=now(), updated_at=now()
                WHERE request_id=$1
                """,
                request_id, str(exc)[:2000],
            )
            raise
        except Exception as exc:
            await self.db.execute(
                """
                UPDATE aios.inference_request SET status='failed',
                    error=$2, completed_at=now(), updated_at=now()
                WHERE request_id=$1
                """,
                request_id, str(exc)[:2000],
            )
            raise
