"""Lease-scoped access to prior work, without granting character-memory sharing."""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

from .work_evidence import WorkEvidence, WorkResultQuery
from aios_app.hud.work_results import compact


class WorkerWorkHistoryQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    worker_key: str
    claim_token: UUID


class WorkerWorkResultQuery(WorkResultQuery):
    worker_key: str
    claim_token: UUID

    @model_validator(mode='after')
    def continued_revision(self):
        if self.cursor and self.revision is None:
            raise ValueError('Continue retrieval with the returned revision')
        return self


class ScopedWorkEvidence(WorkEvidence):
    def __init__(self, db, binding_id, run, origin):
        super().__init__(db)
        self.binding_id, self.run, self.origin = binding_id, run, origin

    async def rows(self, instance_id, *, request_id=None, run_id=None):
        # Scope comes exclusively from the active lease/link. Both direct
        # delegations and chat-admitted work can have useful retained reports.
        return await self.db.fetch(
            """WITH RECURSIVE visible(node_id,depth) AS (
                 SELECT $1::uuid,0
                 UNION SELECT e.parent_node_id,v.depth+1 FROM visible v
                   JOIN aios.dag_edge e ON e.child_node_id=v.node_id
                   WHERE e.timeline_id=$2 AND v.depth<512
               )
               SELECT w.request_id,w.run_id,w.status,w.result,w.objective,
                      w.updated_at AS reported_at,
                      (w.assignment->>'goal_id')::uuid AS goal_id,
                      (w.assignment->>'attempt_id')::uuid AS attempt_id
               FROM aios.workspace_run w
               JOIN aios.dag_node src ON src.node_id=w.origin_source_node_id
                 AND src.timeline_id=$2
               LEFT JOIN aios.character_work_request wr ON wr.run_id=w.run_id
               WHERE w.binding_id=$3 AND w.environment_link_id=$4
                 AND w.origin_instance_id=$5 AND w.origin_source_timeline_id=$2
                 AND w.run_id<>$6 AND w.created_at<=$7
                 AND w.status IN ('finished','failed','cancelled','interrupted')
                 AND (wr.request_id IS NULL OR wr.status NOT IN ('denied','invalidated'))
                 AND EXISTS(SELECT 1 FROM visible WHERE node_id=w.origin_source_node_id)
                 AND NOT EXISTS(SELECT 1 FROM aios.ingest_event ie
                                WHERE ie.event_id=src.event_id AND ie.superseded_at IS NOT NULL)
                 AND ($8::uuid IS NULL OR w.request_id=$8)
                 AND ($9::uuid IS NULL OR w.run_id=$9)
               ORDER BY (COALESCE(w.assignment->>'goal_id','')=$10 AND $10<>'') DESC,
                        (COALESCE(w.assignment->>'attempt_id','')=$11 AND $11<>'') DESC,
                        w.updated_at DESC,w.run_id DESC LIMIT 5""",
            self.origin['source_head_node_id'], self.run['origin_source_timeline_id'],
            self.binding_id, self.run['environment_link_id'], instance_id,
            self.run['run_id'], self.run['created_at'], request_id, run_id,
            str(self.run_spec().get('goal_id') or ''),
            str(self.run_spec().get('attempt_id') or ''),
        )

    def run_spec(self):
        from .workspaces import decode
        return decode(self.run['assignment']) or {}


async def worker_work(db, binding_id, run_id, req, *, read=False):
    """Lock current authority and origin while selecting/projecting evidence."""
    from .workspaces import WorkspaceStore
    from .environment_links import EnvironmentStore

    async with db.atomic():
        store = WorkspaceStore(db)
        binding, run = await store.owned(binding_id, run_id, req)
        await store.current(binding)
        if run['environment_link_id'] is None:
            if read:
                raise HTTPException(404, 'No prior work in this execution scope')
            return {'status': 'unavailable', 'runs': [], 'reason': 'No linked work scope'}
        environments = EnvironmentStore(db)
        link = await environments.link(binding_id, run['environment_link_id'])
        origin = await environments.origin(link, ancestor_node_id=run['origin_source_node_id'])
        evidence = ScopedWorkEvidence(db, binding_id, run, origin)
        owner = run['origin_instance_id']
        if read:
            query = WorkResultQuery.model_validate(req.model_dump(exclude={'worker_key', 'claim_token'}))
            return await evidence.read(owner, query)
        rows = await evidence.rows(owner)
        runs = []
        for row in rows:
            _, view, revision = evidence.projection(row)
            runs.append({
                'run_id': str(row['run_id']), 'request_id': str(row['request_id']),
                'objective': compact(row['objective'], 200),
                'execution_status': row['status'], 'revision': revision,
                'reported_at': str(row['reported_at']),
                'goal_id': str(row['goal_id']) if row['goal_id'] else None,
                'attempt_id': str(row['attempt_id']) if row['attempt_id'] else None,
                'summary': compact(view['summary'] or view['report_excerpt'], 360),
                'findings': [compact(x, 300) for x in view['findings'][:2]],
                'limitations': [compact(x, 160) for x in view['limitations'][:2]],
                'evidence_source': view['evidence_source'], 'not_goal_completion': True,
            })
        return {'status': 'retrieved', 'runs': runs,
                'selection': 'same goal/attempt first, then recent; at most five prior terminal runs',
                'evidence_source': 'worker-reported historical evidence; not current measurements'}
