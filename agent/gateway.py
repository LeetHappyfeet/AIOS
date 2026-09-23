from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib import request as urllib_request
from uuid import UUID

from aios_app.db import Database
from .runtime import AgentRuntimeStore


class ExternalGateway:
    def __init__(self, db: Database):
        self.db = db
        self.runtime = AgentRuntimeStore(db)

    async def register(
        self, *, integration_key: str, integration_type: str, display_name: str,
        base_url: str | None = None, auth_env: str | None = None,
        capabilities: list[str] | None = None, config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.external_integration
               (integration_key,integration_type,display_name,base_url,auth_env,capabilities,config)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb)
               ON CONFLICT (integration_key) DO UPDATE SET
                 integration_type=EXCLUDED.integration_type, display_name=EXCLUDED.display_name,
                 base_url=EXCLUDED.base_url, auth_env=EXCLUDED.auth_env,
                 capabilities=EXCLUDED.capabilities, config=EXCLUDED.config, updated_at=now()
               RETURNING *""",
            integration_key, integration_type, display_name, base_url, auth_env,
            json.dumps(capabilities or []), json.dumps(dict(config or {})),
        )
        return dict(row)

    async def ingest_event(
        self, *, integration_key: str, external_event_id: str, instance_id: UUID,
        event_type: str, payload: Mapping[str, Any],
    ) -> UUID:
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.external_event_receipt
               (integration_id,external_event_id,instance_id,event_type,payload)
               SELECT integration_id,$2,$3,$4,$5::jsonb FROM aios.external_integration
               WHERE integration_key=$1 AND enabled=true
               ON CONFLICT (integration_id,external_event_id,instance_id) DO NOTHING
               RETURNING receipt_id""",
            integration_key, external_event_id, instance_id, event_type,
            json.dumps(dict(payload), default=str),
        )
        if not row:
            existing = await self.db.fetchrow(
                """SELECT r.receipt_id FROM aios.external_event_receipt r
                   JOIN aios.external_integration i ON i.integration_id=r.integration_id
                   WHERE i.integration_key=$1 AND r.external_event_id=$2 AND r.instance_id=$3""",
                integration_key, external_event_id, instance_id,
            )
            if not existing:
                raise LookupError("integration disabled, unknown, or event unavailable")
            return existing["receipt_id"]
        receipt_id = row["receipt_id"]
        await self.runtime.wake(
            instance_id=instance_id, event_type=event_type,
            source_type=f"integration:{integration_key}", source_id=external_event_id,
            payload={"receipt_id": str(receipt_id), **dict(payload)},
            dedupe_key=f"external:{integration_key}:{external_event_id}",
        )
        return receipt_id

    async def queue_delivery(
        self, *, action_id: UUID, integration_key: str, payload: Mapping[str, Any],
    ) -> UUID:
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.external_action_delivery
               (action_id,integration_id,correlation_key,request_payload)
               SELECT $1,integration_id,$2,$3::jsonb FROM aios.external_integration
               WHERE integration_key=$4 AND enabled=true
               ON CONFLICT (integration_id,correlation_key) DO UPDATE
                 SET request_payload=EXCLUDED.request_payload
               RETURNING delivery_id""",
            action_id, str(action_id), json.dumps(dict(payload), default=str), integration_key,
        )
        if not row:
            raise LookupError(f"Integration '{integration_key}' is unavailable")
        return row["delivery_id"]

    async def deliver(self, delivery_id: UUID) -> dict[str, Any]:
        row = await self.db.fetchrow(
            """SELECT d.*,i.base_url,i.auth_env,i.integration_key
               FROM aios.external_action_delivery d JOIN aios.external_integration i USING(integration_id)
               WHERE d.delivery_id=$1""",
            delivery_id,
        )
        if not row:
            raise LookupError("unknown external delivery")
        if not row["base_url"]:
            raise RuntimeError("integration has no delivery URL")
        payload = row["request_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        headers = {"Content-Type": "application/json", "Idempotency-Key": row["correlation_key"]}
        if row["auth_env"] and os.getenv(row["auth_env"]):
            headers["Authorization"] = "Bearer " + os.environ[row["auth_env"]]
        req = urllib_request.Request(
            row["base_url"], data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=30) as response:
                body = response.read(1024 * 1024).decode("utf-8", errors="replace")
                result = {"status_code": response.status, "body": body}
            await self.db.execute(
                """UPDATE aios.external_action_delivery SET status='succeeded',
                   response_payload=$2::jsonb,completed_at=now() WHERE delivery_id=$1""",
                delivery_id, json.dumps(result),
            )
            return result
        except Exception as exc:
            await self.db.execute(
                """UPDATE aios.external_action_delivery SET status='failed',error=$2,
                   completed_at=now() WHERE delivery_id=$1""",
                delivery_id, str(exc)[:2000],
            )
            raise
