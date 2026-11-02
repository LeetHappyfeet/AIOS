from copy import deepcopy
from uuid import uuid4

import pytest

from aios_app.hud.delivery import delivery_response


def sample():
    instance, timeline, head = uuid4(), uuid4(), uuid4()
    state = dict(source_timeline_id=timeline, source_head_node_id=head, state_version=3,
                 cognitive_revision='c1', profile_revision='p1')
    frame = {'hud': {'generation_ready': True, 'cognitive_revision': 'c1', 'profile_revision': 'p1',
                     'task_event_watermark': 9, 'freshness': {
                         'source_head_node_id': str(head), 'source_timeline_id': str(timeline),
                         'runtime_state_version': 3, 'runtime_current': True, 'source_current': True}},
             'work': {'requests': []}, 'remote_tasks': [{'run_id': 'a', 'sequence': 9, 'status': 'running'}]}
    return instance, frame, state


def render(frame):
    return f"ready={frame['hud']['generation_ready']}"


def test_delivery_exposes_consistent_freshness_and_stable_revision():
    instance, frame, state = sample()
    first = delivery_response(instance, frame, state, render)
    second = delivery_response(instance, frame, state, render)
    assert first['hud_revision'] == second['hud_revision']
    assert first['freshness']['hud_revision'] == first['hud_revision']
    assert first['task_event_watermark'] == 9
    assert first['freshness']['cognitive_revision'] == 'c1'
    assert first['generation_ready'] and first['text'] == 'ready=True'


def test_per_run_sequences_cannot_hide_a_different_task_completion():
    instance, frame, state = sample()
    before = delivery_response(instance, deepcopy(frame), state, render)
    frame['remote_tasks'].append({'run_id': 'b', 'sequence': 2, 'status': 'completed'})
    after = delivery_response(instance, frame, state, render)
    assert before['task_event_watermark'] == after['task_event_watermark']
    assert before['hud_revision'] != after['hud_revision']


@pytest.mark.parametrize('field,value', [
    ('source_head_node_id', uuid4()), ('source_timeline_id', uuid4()),
    ('state_version', 4), ('cognitive_revision', 'c2'), ('profile_revision', 'p2'),
])
def test_runtime_change_during_overlay_never_returns_a_ready_hud(field, value):
    instance, frame, state = sample()
    state[field] = value
    response = delivery_response(instance, frame, state, render)
    assert not response['generation_ready']
    assert not response['freshness']['delivery_current']
    assert response['text'] == 'ready=False'


def test_work_admission_and_canonical_text_also_change_revision():
    instance, frame, state = sample()
    before = delivery_response(instance, deepcopy(frame), state, render)
    frame['work']['requests'].append({'status': 'needs_configuration'})
    after = delivery_response(instance, deepcopy(frame), state, render)
    changed_text = delivery_response(instance, frame, state, lambda f: 'new text')
    assert len({r['hud_revision'] for r in [before, after, changed_text]}) == 3


@pytest.mark.asyncio
async def test_public_hud_delivery_refreshes_overlays_without_mutating_cached_frame(monkeypatch):
    from unittest.mock import AsyncMock
    import aios_app.main_legacy as main
    import aios_app.agent.environment_links as links
    from aios_app.agent.work_admission import WorkAdmission
    instance, cached, state = sample()
    original = deepcopy(cached)
    monkeypatch.setattr(main.world_runtime, 'prepare_frame', AsyncMock(return_value=cached))
    monkeypatch.setattr(main.world_runtime, 'get_state', AsyncMock(return_value=state))
    monkeypatch.setattr(WorkAdmission, 'overlay', AsyncMock(return_value={'requests': []}))
    tasks = AsyncMock(return_value=[{'run_id': 'a', 'sequence': 9, 'status': 'running'}])
    monkeypatch.setattr(links, 'task_overlay', tasks)
    monkeypatch.setattr(main, 'render_hud_text', render)
    async def deliver():
        return await main._prepare_hud_response(instance, through_node_id=state['source_head_node_id'],
            recent_limit=12, token_budget=4000, wait_ms=0)
    before = await deliver()
    tasks.return_value = [{'run_id': 'a', 'sequence': 9, 'status': 'running'},
                          {'run_id': 'b', 'sequence': 2, 'status': 'completed'}]
    after = await deliver()
    assert before['hud_revision'] != after['hud_revision']
    assert cached == original
    assert main.world_runtime.get_state.await_count == 2
    assert before['generation_ready'] and after['generation_ready']
    state['source_head_node_id'] = uuid4()
    changed = await deliver()
    assert not changed['generation_ready'] and changed['text'] == 'ready=False'
