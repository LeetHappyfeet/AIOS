"""Real PostgreSQL transaction/race tests; uses a disposable database.

Set AIOS_OUTCOME_TEST_DSN to a PostgreSQL database whose role may CREATE DATABASE.
No application database is modified. The fixture creates/drops its own database.
"""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService
from aios_app.agent.outcomes import OutcomeResolver
from aios_app.agent.reinforcement import ShadowReinforcementService
from aios_app.epistemic.goal_scene_reconciliation import SceneGoalReconciler

DSN = os.getenv('AIOS_OUTCOME_TEST_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='isolated PostgreSQL test DSN not configured')
ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = '''
CREATE SCHEMA aios;
CREATE TABLE aios.character_instance(instance_id uuid PRIMARY KEY);
CREATE TABLE aios.dag_node(node_id uuid PRIMARY KEY,timeline_id uuid);
CREATE TABLE aios.character_agent_goal(goal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 instance_id uuid REFERENCES aios.character_instance,goal_text text,status text DEFAULT 'active',
 priority integer DEFAULT 100,meta jsonb DEFAULT '{}',source_task_id uuid,source_node_id uuid,
 root_task_id uuid,source_action_id uuid,completed_at timestamptz,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now());
CREATE TABLE aios.character_temporal_trigger(trigger_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),goal_id uuid,
 status text,due_at timestamptz,cancelled_at timestamptz,last_evaluated_at timestamptz,last_outcome text,updated_at timestamptz DEFAULT now());
CREATE TABLE aios.character_cognitive_thread(thread_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),instance_id uuid,goal_id uuid,
 status text,pressure integer DEFAULT 1,resolved_at timestamptz,meta jsonb DEFAULT '{}',updated_at timestamptz DEFAULT now());
CREATE TABLE aios.character_runtime_state(instance_id uuid PRIMARY KEY,state_version integer DEFAULT 1,updated_at timestamptz);
CREATE TABLE aios.character_hud_readiness(instance_id uuid PRIMARY KEY,status text,dirty_since timestamptz,updated_at timestamptz);
CREATE TABLE aios.character_goal_evidence(goal_id uuid,instance_id uuid,evidence_type text,relation text,evidence_id text,
 source_node_id uuid,confidence float,meta jsonb);
CREATE TABLE aios.character_action(action_id uuid PRIMARY KEY,instance_id uuid,arguments jsonb,status text,created_at timestamptz DEFAULT now());
CREATE TABLE aios.character_cognitive_operation(operation_id uuid PRIMARY KEY,instance_id uuid,input jsonb,status text,created_at timestamptz DEFAULT now());
CREATE TABLE aios.knowledge_acquisition_event(acquisition_id uuid PRIMARY KEY,instance_id uuid,dag_node_id uuid,meta jsonb,created_at timestamptz DEFAULT now());
CREATE TABLE aios.epistemic_authority_admission(acquisition_id uuid PRIMARY KEY,origin_kind text,authority_state text,authorized_uses text[]);
'''


class TestDatabase(Database):
    __test__ = False

    async def fetchval(self, sql, *args):
        async with self.connection() as con:
            return await con.fetchval(sql, *args)


@pytest_asyncio.fixture
async def db():
    admin = await asyncpg.connect(DSN)
    name = 'aios_outcomes_test_' + uuid4().hex
    await admin.execute(f'CREATE DATABASE {name}')
    pool = await asyncpg.create_pool(DSN, database=name, min_size=1, max_size=8)
    database = TestDatabase(DSN)
    database.pool = pool
    try:
        async with pool.acquire() as con:
            await con.execute(BOOTSTRAP)
            for path in ['20260929_05_character_outcomes.sql', '20260929_06_shadow_reinforcement.sql']:
                await con.execute((ROOT/'migrations/current'/path).read_text())
        yield database
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE {name}')
        await admin.close()


async def new_goal(db, meta=None):
    instance, node, timeline = uuid4(), uuid4(), uuid4()
    await db.execute('INSERT INTO aios.character_instance VALUES($1)', instance)
    await db.execute('INSERT INTO aios.dag_node VALUES($1,$2)', node, timeline)
    await db.execute('INSERT INTO aios.character_runtime_state(instance_id) VALUES($1)', instance)
    await db.execute("INSERT INTO aios.character_hud_readiness(instance_id,status) VALUES($1,'ready')", instance)
    row = await db.fetchrow("INSERT INTO aios.character_agent_goal(instance_id,goal_text,source_node_id,meta) VALUES($1,'Buy jacket',$2,$3::jsonb) RETURNING goal_id",
                            instance, node, json.dumps(meta or {}))
    return instance, row['goal_id'], node, timeline


@pytest.mark.asyncio
async def test_competing_closures_emit_one_outcome_and_cleanup(db):
    instance, goal, _, _ = await new_goal(db)
    await db.execute("INSERT INTO aios.character_temporal_trigger(goal_id,status) VALUES($1,'firing')", goal)
    await db.execute("INSERT INTO aios.character_cognitive_thread(instance_id,goal_id,status) VALUES($1,$2,'working')", instance, goal)
    service = CharacterGoalService(db)
    results = await asyncio.gather(*[service.finish(instance_id=instance, goal_id=goal, status=s, refresh_scene=False)
                                    for s in ['completed', 'failed']], return_exceptions=True)
    assert sum(isinstance(x, LookupError) for x in results) == 1
    assert await db.fetchval('SELECT count(*) FROM aios.character_outcome_event') == 1
    assert await db.fetchval('SELECT count(*) FROM aios.character_appraisal_pending') == 1
    assert await db.fetchval('SELECT status FROM aios.character_temporal_trigger') == 'cancelled'
    assert await db.fetchval('SELECT status FROM aios.character_cognitive_thread') == 'resolved'
    with pytest.raises(asyncpg.RaiseError):
        await db.execute("UPDATE aios.character_outcome_event SET verification='verified'")
    with pytest.raises(asyncpg.RaiseError):
        await db.execute("UPDATE aios.character_agent_goal SET status='active'")


@pytest.mark.asyncio
async def test_outer_transaction_rolls_back_goal_outcome_and_cleanup(db):
    instance, goal, _, _ = await new_goal(db)
    async with db.connection() as con:
        with pytest.raises(RuntimeError):
            async with con.transaction():
                await CharacterGoalService(con).finish(instance_id=instance, goal_id=goal, status='failed', refresh_scene=False)
                raise RuntimeError('crash')
    assert await db.fetchval('SELECT status FROM aios.character_agent_goal') == 'active'
    assert await db.fetchval('SELECT count(*) FROM aios.character_outcome_event') == 0
    assert await db.fetchval('SELECT closed_at FROM aios.character_goal_attempt') is None


@pytest.mark.asyncio
async def test_timer_cycles_keep_attempt_and_explicit_retry_is_idempotent(db):
    instance, goal, _, _ = await new_goal(db)
    initial = await db.fetchval('SELECT attempt_id FROM aios.character_goal_attempt')
    await db.execute("UPDATE aios.character_agent_goal SET status='scheduled' WHERE goal_id=$1", goal)
    await db.execute("UPDATE aios.character_agent_goal SET status='active' WHERE goal_id=$1", goal)
    assert await db.fetchval('SELECT attempt_id FROM aios.character_goal_attempt') == initial
    service = CharacterGoalService(db)
    await service.finish(instance_id=instance, goal_id=goal, status='failed', refresh_scene=False)
    request = uuid4()
    await asyncio.gather(*[service.retry(instance_id=instance, goal_id=goal, request_id=request, refresh_scene=False) for _ in range(2)])
    assert await db.fetchval('SELECT count(*) FROM aios.character_goal_attempt') == 2
    await service.finish(instance_id=instance, goal_id=goal, status='completed', refresh_scene=False)
    await service.retry(instance_id=instance, goal_id=goal, request_id=request, refresh_scene=False)
    assert await db.fetchval('SELECT status FROM aios.character_agent_goal') == 'completed'
    assert await db.fetchval('SELECT count(*) FROM aios.character_goal_attempt') == 2


async def typed_receipt(db, instance, goal, node, *, outcome='success', origin='deterministic_tool', attribution='self'):
    attempt = await db.fetchval('SELECT attempt_id FROM aios.character_goal_attempt WHERE goal_id=$1 ORDER BY attempt_number DESC LIMIT 1', goal)
    strategy_receipt = await db.fetchval('SELECT receipt_id FROM aios.character_attempt_strategy WHERE attempt_id=$1', attempt)
    acquisition = uuid4()
    await db.execute('INSERT INTO aios.knowledge_acquisition_event(acquisition_id,instance_id,dag_node_id,meta) VALUES($1,$2,$3,$4::jsonb)',
        acquisition, instance, node, json.dumps({'outcome_receipt': {'goal_id': str(goal), 'attempt_id': str(attempt),
            'outcome': outcome, 'attribution': attribution, 'strategy_receipt_id': str(strategy_receipt) if strategy_receipt else None, 'cause_class': 'achieved' if outcome=='success' else 'execution_failure'}}))
    await db.execute("INSERT INTO aios.epistemic_authority_admission VALUES($1,$2,'canonical',ARRAY['action_precondition'])", acquisition, origin)
    return acquisition


@pytest.mark.asyncio
async def test_authorized_credit_replay_correction_and_rebuild(db):
    instance, goal, node, _ = await new_goal(db, {'expectation': {'success_probability': .5, 'importance': 1}})
    resolver = OutcomeResolver(db)
    operation = uuid4()
    await db.execute("INSERT INTO aios.character_cognitive_operation(operation_id,instance_id,input,status) VALUES($1,$2,$3::jsonb,'succeeded')",
                     operation, instance, json.dumps({'goal_id': str(goal)}))
    await resolver.link_strategy(instance_id=instance, goal_id=goal, strategy_key='memory-first', receipt_type='cognitive_operation', receipt_id=operation)
    acquisition = await typed_receipt(db, instance, goal, node)
    oid = await resolver.resolve_receipt(instance_id=instance, acquisition_id=acquisition)
    assert await resolver.resolve_receipt(instance_id=instance, acquisition_id=acquisition) == oid
    shadows = ShadowReinforcementService(db)
    await asyncio.gather(shadows.process_pending(), shadows.process_pending())
    projection = await db.fetchrow('SELECT * FROM aios.character_behavior_projection')
    assert projection['success_count'] == 1
    assert projection['signal_mean'] == .125
    count = await db.fetchval('SELECT count(*) FROM aios.character_reinforcement_event')
    assert await shadows.process_pending() == 0
    inspection = await shadows.inspect(instance)
    assert inspection['shadow'] is True and inspection['pending'] == 0
    assert len(inspection['projections']) == 1
    assert await db.fetchval('SELECT count(*) FROM aios.character_reinforcement_event') == count
    request = uuid4()
    corrected = await resolver.invalidate(instance_id=instance, outcome_id=oid, request_id=request, reason='receipt retracted')
    assert await resolver.invalidate(instance_id=instance, outcome_id=oid, request_id=request, reason='receipt retracted') == corrected
    await shadows.process_pending()
    await shadows.rebuild(instance)
    assert await db.fetchval('SELECT count(*) FROM aios.character_behavior_projection') == 0
    assert await db.fetchval('SELECT count(*) FROM aios.character_outcome_event') == 3


@pytest.mark.asyncio
async def test_generated_receipt_cannot_upgrade_learning_authority(db):
    instance, goal, node, _ = await new_goal(db)
    acquisition = await typed_receipt(db, instance, goal, node, origin='model_generation')
    with pytest.raises(PermissionError):
        await OutcomeResolver(db).resolve_receipt(instance_id=instance, acquisition_id=acquisition)
    assert await db.fetchval('SELECT status FROM aios.character_agent_goal') == 'active'
    assert await db.fetchval('SELECT count(*) FROM aios.character_outcome_event') == 0


@pytest.mark.asyncio
async def test_scene_failure_resolves_scheduled_goal_without_verified_credit(db):
    instance, goal, node, _ = await new_goal(db, {'failure_contract': {'slot': 'store_open', 'value': False}})
    await db.execute("UPDATE aios.character_agent_goal SET status='scheduled' WHERE goal_id=$1", goal)
    reconciler = SceneGoalReconciler(db)
    assert await reconciler.reconcile(instance_id=instance, scene={'store_open': False}, source_node_id=node) == [goal]
    assert await db.fetchval('SELECT status FROM aios.character_agent_goal') == 'failed'
    assert await db.fetchval('SELECT verification FROM aios.character_outcome_event') == 'projected'
    await ShadowReinforcementService(db).process_pending()
    assert await db.fetchval('SELECT magnitude FROM aios.character_reinforcement_event') == 0


@pytest.mark.asyncio
async def test_contracts_frozen_conflicts_unresolved_and_timeline_isolation(db):
    contract = {'slot': 'store_open', 'value': True}
    instance, goal, node, _ = await new_goal(db, {'goal_contract': {'success': contract, 'failure': contract}})
    with pytest.raises(asyncpg.RaiseError):
        await db.execute("UPDATE aios.character_agent_goal SET meta=meta||'{\"failure_contract\": {}}'::jsonb WHERE goal_id=$1", goal)
    assert await SceneGoalReconciler(db).reconcile(instance_id=instance, scene={'store_open': True}, source_node_id=node) == []
    wrong = uuid4()
    await db.execute('INSERT INTO aios.dag_node VALUES($1,$2)', wrong, uuid4())
    acquisition = await typed_receipt(db, instance, goal, wrong)
    with pytest.raises(ValueError):
        await OutcomeResolver(db).resolve_receipt(instance_id=instance, acquisition_id=acquisition)


@pytest.mark.asyncio
async def test_late_review_cannot_close_a_retry_attempt(db):
    from aios_app.agent.cognitive_lifecycle import CognitiveLifecycleReconciler
    instance, goal, _, _ = await new_goal(db)
    operation = uuid4()
    await db.execute("INSERT INTO aios.character_cognitive_operation(operation_id,instance_id,input,status) VALUES($1,$2,$3::jsonb,'succeeded')",
                     operation, instance, json.dumps({'goal_id': str(goal)}))
    old = dict(await db.fetchrow('SELECT * FROM aios.character_cognitive_operation WHERE operation_id=$1', operation))
    old['operation_type'] = 'planning.review'
    old['thread_id'] = None
    service = CharacterGoalService(db)
    await service.finish(instance_id=instance, goal_id=goal, status='failed', refresh_scene=False)
    await service.retry(instance_id=instance, goal_id=goal, request_id=uuid4(), refresh_scene=False)
    await CognitiveLifecycleReconciler(db).reconcile_operation(operation=old, result={'kind': 'choice', 'option_index': 2})
    assert await db.fetchval('SELECT status FROM aios.character_agent_goal') == 'active'
    assert await db.fetchval('SELECT count(*) FROM aios.character_outcome_event') == 1
    with pytest.raises(LookupError):
        await service.finish(instance_id=instance, goal_id=goal, status='completed', attempt_id=old['goal_attempt_id'], refresh_scene=False)


@pytest.mark.asyncio
async def test_receipt_intake_is_durable_and_provenance_cannot_be_rewritten(db):
    instance, goal, node, _ = await new_goal(db)
    acquisition = await typed_receipt(db, instance, goal, node)
    resolver = OutcomeResolver(db)
    assert await resolver.process_receipts() == 1
    assert await resolver.process_receipts() == 0
    assert await db.fetchval('SELECT status FROM aios.character_outcome_receipt') == 'accepted'
    with pytest.raises(asyncpg.RaiseError):
        await db.execute("UPDATE aios.character_outcome_receipt SET receipt='{}'::jsonb")
    # Mutable acquisition metadata cannot change the captured outcome receipt.
    await db.execute("UPDATE aios.knowledge_acquisition_event SET meta='{}'::jsonb WHERE acquisition_id=$1", acquisition)
    assert await resolver.resolve_receipt(instance_id=instance, acquisition_id=acquisition)
    with pytest.raises(asyncpg.RaiseError):
        await db.execute("UPDATE aios.character_goal_attempt SET expectation_snapshot='{\"importance\":1}'::jsonb")


@pytest.mark.asyncio
async def test_delayed_old_receipt_corrects_history_without_closing_retry(db):
    instance, goal, node, _ = await new_goal(db)
    acquisition = await typed_receipt(db, instance, goal, node, outcome='failure')
    service = CharacterGoalService(db)
    await service.finish(instance_id=instance, goal_id=goal, status='cancelled', refresh_scene=False)
    await service.retry(instance_id=instance, goal_id=goal, request_id=uuid4(), refresh_scene=False)
    await OutcomeResolver(db).resolve_receipt(instance_id=instance, acquisition_id=acquisition)
    assert await db.fetchval('SELECT status FROM aios.character_agent_goal') == 'active'
    assert await db.fetchval('SELECT count(*) FROM aios.character_goal_attempt WHERE closed_at IS NULL') == 1
    await ShadowReinforcementService(db).process_pending()
    assert await db.fetchval('SELECT count(*) FROM aios.character_behavior_projection') == 0


@pytest.mark.asyncio
async def test_success_without_explicit_credit_receipt_is_neutral(db):
    instance, goal, node, _ = await new_goal(db)
    operation = uuid4()
    await db.execute("INSERT INTO aios.character_cognitive_operation(operation_id,instance_id,input,status) VALUES($1,$2,$3::jsonb,'succeeded')",
                     operation, instance, json.dumps({'goal_id': str(goal)}))
    resolver = OutcomeResolver(db)
    await resolver.link_strategy(instance_id=instance, goal_id=goal, strategy_key='memory-first', receipt_type='cognitive_operation', receipt_id=operation)
    # Self attribution without a matching execution credit is insufficient.
    second = uuid4()
    attempt = await db.fetchval('SELECT attempt_id FROM aios.character_goal_attempt')
    await db.execute('INSERT INTO aios.knowledge_acquisition_event(acquisition_id,instance_id,dag_node_id,meta) VALUES($1,$2,$3,$4::jsonb)',
        second, instance, node, json.dumps({'outcome_receipt': {'goal_id': str(goal), 'attempt_id': str(attempt),
        'outcome': 'success', 'attribution': 'self', 'cause_class': 'achieved', 'strategy_receipt_id': str(uuid4())}}))
    await db.execute("INSERT INTO aios.epistemic_authority_admission VALUES($1,'deterministic_tool','canonical',ARRAY['action_precondition'])", second)
    await resolver.resolve_receipt(instance_id=instance, acquisition_id=second)
    await ShadowReinforcementService(db).process_pending()
    assert await db.fetchval('SELECT count(*) FROM aios.character_behavior_projection') == 0
