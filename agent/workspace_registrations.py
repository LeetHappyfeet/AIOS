"""Persistent workers, explicit standing authorization and branch-scoped attachments."""
from __future__ import annotations

import hashlib
import logging
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


class RegistrationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace_key: str = Field(min_length=1, max_length=160)
    worker_key: str = Field(min_length=1, max_length=160)
    character_id: str = Field(min_length=1, max_length=160)
    user_name: str = Field(min_length=1, max_length=160)
    scope_key: str = Field(default='conversation', min_length=1, max_length=160)
    root_hint: str = Field(min_length=1, max_length=1000)


class AuthorizationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    disposition: Literal['allow', 'require_approval', 'deny'] = 'require_approval'


class RegistrationStore:
    def __init__(self, db):
        self.db = db

    async def register(self, req):
        from .workspaces import public
        async with self.db.atomic():
            await self.db.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", 'registration:' + req.workspace_key)
            old = await self.db.fetchrow('SELECT * FROM aios.workspace_registration WHERE workspace_key=$1', req.workspace_key)
            if old:
                mismatches = [k for k in type(req).model_fields if old[k] != getattr(req, k)]
                if mismatches:
                    raise HTTPException(409, 'Registered workspace identity changed: ' + ', '.join(mismatches))
                return public(old)
            row = await self.db.execute_returning_row('''INSERT INTO aios.workspace_registration
                (workspace_key,worker_key,character_id,user_name,scope_key,root_hint)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *''',
                req.workspace_key, req.worker_key, req.character_id, req.user_name, req.scope_key, req.root_hint)
            return public(row)

    async def get(self, registration_id, worker_key=None, *, lock=False):
        row = await self.db.fetchrow('SELECT * FROM aios.workspace_registration WHERE registration_id=$1' + (' FOR UPDATE' if lock else ''), registration_id)
        if not row or (worker_key is not None and row['worker_key'] != worker_key):
            raise HTTPException(404, 'Unknown workspace registration or worker')
        return row

    async def listing(self):
        from .workspaces import public
        return [public(r) for r in await self.db.fetch('''SELECT *,
            last_seen_at>now()-interval '45 seconds' AS worker_online
            FROM aios.workspace_registration ORDER BY created_at''')]

    async def authorize(self, registration_id, req):
        """Operator-only; worker credentials cannot broaden standing authority."""
        from .environment_links import EnvironmentStore
        from .workspaces import public
        async with self.db.atomic():
            r = await self.get(registration_id, lock=True)
            # Serialize authorization changes across registrations for this owner.
            await self.db.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"registration-owner:{r['character_id']}:{r['user_name']}:{r['scope_key']}")
            if req.enabled and await self.db.fetchval('''SELECT EXISTS(SELECT 1 FROM aios.workspace_registration
                WHERE enabled AND character_id=$1 AND user_name=$2 AND scope_key=$3 AND registration_id<>$4)''',
                r['character_id'], r['user_name'], r['scope_key'], registration_id):
                raise HTTPException(409, 'Another workspace is authorized for this character/human/scope; disable it first')
            row = await self.db.execute_returning_row('''UPDATE aios.workspace_registration
                SET enabled=$2,disposition=$3 WHERE registration_id=$1 RETURNING *''', registration_id, req.enabled, req.disposition)
            # Standing authority changes must affect existing attachments too. Explicit
            # per-instance policies remain an additional, potentially stricter gate.
            links = await self.db.fetch('''SELECT l.* FROM aios.character_environment_link l
                JOIN aios.workspace_binding b ON b.binding_id=l.execution_binding_id
                WHERE b.registration_id=$1 AND l.enabled ORDER BY l.link_id''', registration_id)
            if not req.enabled or req.disposition == 'deny':
                for link in links:
                    await EnvironmentStore(self.db).revoke(link['execution_binding_id'], link['link_id'])
            return public(row)

    async def ensure(self, instance_id):
        """Attach under existing authorization. Never move/revive/replace a binding."""
        from .workspace_setup import WorkspaceSetup
        from .workspaces import WorkspaceStore, BindingIn
        from .environment_links import EnvironmentStore, LinkIn
        from .work_admission import WorkAdmission
        from .work_profiles import experimental_profile
        async with self.db.atomic():
            try:
                origin = await WorkspaceSetup(self.db).origin(instance_id)
            except HTTPException:
                return
            if not origin['source_timeline_id'] or not origin['source_head_node_id']:
                return
            r = await self.db.fetchrow('''SELECT * FROM aios.workspace_registration WHERE enabled
                AND disposition<>'deny' AND character_id=$1 AND user_name=$2 AND scope_key=$3 FOR UPDATE''',
                origin['character_id'], origin['user_name'], origin['scope_key'])
            if not r:
                return
            await self.db.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"registration-instance:{instance_id}")
            b = await self.db.fetchrow('SELECT * FROM aios.workspace_binding WHERE instance_id=$1 FOR UPDATE', instance_id)
            if b:
                # Matching legacy bindings may be explicitly adopted by standing authorization.
                if any(b[k] != r[k] for k in ('worker_key','character_id','user_name','scope_key','root_hint')):
                    raise HTTPException(409, 'This chat has a legacy binding with a different worker or workspace path; restore that identity or reconcile the binding in Workspace Delegation')
                if b['registration_id'] not in (None, r['registration_id']):
                    raise HTTPException(409, 'This chat belongs to another workspace registration; explicit reconciliation is required')
                if b['session_id'] != origin['session_id'] or b['timeline_id'] != origin['source_timeline_id']:
                    raise HTTPException(409, 'Chat source changed; automatic routing will not move an existing binding')
            else:
                key = hashlib.sha256(f"{r['registration_id']}:{instance_id}".encode()).hexdigest()
                b = await WorkspaceStore(self.db).bind(BindingIn(
                    workspace_key=key, worker_key=r['worker_key'], character_id=r['character_id'],
                    instance_id=instance_id, session_id=origin['session_id'], user_name=r['user_name'],
                    scope_key=r['scope_key'], root_hint=r['root_hint']))
                b = await WorkspaceStore(self.db).binding(UUID(b['binding_id']))
            # A revoked link is a tombstone: automation must not resurrect it.
            old = await self.db.fetchrow('''SELECT * FROM aios.character_environment_link
                WHERE execution_binding_id=$1 AND origin_instance_id=$2''', b['binding_id'], instance_id)
            if old and not old['enabled']:
                raise HTTPException(409, 'Workspace link was revoked; automatic routing will not re-enable it')
            link = await EnvironmentStore(self.db).create(b['binding_id'], LinkIn(origin_instance_id=instance_id))
            await self.db.execute('''UPDATE aios.workspace_binding SET registration_id=$2,last_seen_at=$3
                WHERE binding_id=$1''', b['binding_id'], r['registration_id'], r['last_seen_at'])
            # Seed only a missing preset. Never re-enable a manually disabled profile.
            exists = await self.db.fetchval('''SELECT EXISTS(SELECT 1 FROM aios.workspace_work_profile
                WHERE link_id=$1 AND capability='workspace' AND purpose='experimental_task')''', UUID(link['link_id']))
            if not exists:
                await WorkAdmission(self.db).configure(b['binding_id'], UUID(link['link_id']), experimental_profile())

    async def ensure_available(self, instance_id):
        # A conflicting legacy writer must not break HUD delivery or invent routing.
        try:
            return await self.ensure(instance_id)
        except HTTPException as exc:
            log.info('Automatic workspace attachment unavailable instance=%s reason=%s', instance_id, exc.detail)
            return str(exc.detail)

    async def claim(self, registration_id, worker_key):
        from .work_admission import WorkAdmission
        from .workspaces import WorkspaceStore
        async with self.db.atomic():
            r = await self.get(registration_id, worker_key, lock=True)
            await self.db.execute('UPDATE aios.workspace_registration SET last_seen_at=now() WHERE registration_id=$1', registration_id)
            await self.db.execute('UPDATE aios.workspace_binding SET last_seen_at=now() WHERE registration_id=$1', registration_id)
            enabled = r['enabled'] and r['disposition'] != 'deny'
            bindings = await self.db.fetch('SELECT binding_id FROM aios.workspace_binding WHERE registration_id=$1 ORDER BY binding_id', registration_id)
        if not enabled:
            return {'run': None, 'status': 'needs_authorization'}
        # Do not hold the registration lock while admission locks request rows.
        for b in bindings:
            await WorkAdmission(self.db).reconsider(binding_id=b['binding_id'])
        async with self.db.atomic():
            r = await self.get(registration_id, worker_key, lock=True)
            if not r['enabled'] or r['disposition'] == 'deny':
                return {'run': None, 'status': 'needs_authorization'}
            # Serialize all chat bindings sharing one physical workspace.
            if await self.db.fetchval('''SELECT EXISTS(SELECT 1 FROM aios.workspace_run w
                JOIN aios.workspace_binding b USING(binding_id) WHERE b.registration_id=$1
                AND w.status='running' AND w.lease_expires_at>now())''', registration_id):
                return {'run': None, 'status': 'busy'}
            b = await self.db.fetchrow('''SELECT b.binding_id FROM aios.workspace_run w
                JOIN aios.workspace_binding b USING(binding_id) WHERE b.registration_id=$1
                AND w.status IN ('queued','running') ORDER BY (w.status='running') DESC,w.created_at LIMIT 1''', registration_id)
            run = await WorkspaceStore(self.db).claim(b['binding_id'], worker_key, 3) if b else None
            return {'run': run, 'status': 'listening', 'binding_id': str(b['binding_id']) if run else None}
