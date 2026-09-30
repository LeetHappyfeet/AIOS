import asyncio
from uuid import UUID

import pytest
from aios_app.ui.semantic_inspection_inputs import api_error_detail, build_inspection_request, member_seed
from aios_app.semantic_index.inspection import inspect_neighborhood


seed = str(UUID(int=1))
cluster = str(UUID(int=2))


def test_expansion_uses_selected_seed_even_with_previous_cluster_field():
    payload = build_inspection_request('propositions_v1', cluster, seed, 1, 0, 0)
    assert payload['point_ids'] == [seed]
    assert 'cluster_id' not in payload
    assert payload['neighbors'] == 1


def test_cluster_inspection_without_expansion_remains_supported():
    assert build_inspection_request('propositions_v1', cluster, '', 0, 0, 0)['cluster_id'] == cluster


@pytest.mark.parametrize('points,offset', [(f'{seed} {cluster}',0),(seed,1)])
def test_invalid_seed_selection_is_explained_before_api_request(points,offset):
    with pytest.raises(ValueError):
        build_inspection_request('propositions_v1', cluster, points, 1, offset, 0)


def test_selecting_ownership_member_uses_point_id_and_clears_cluster_and_offset():
    result = {'collection':'epistemic_objects_v1','members':[
        {'proposition_id':cluster,'points':[{'point_id':seed}]}]}
    assert member_seed(result,0) == (seed,'','epistemic_objects_v1',0)


def test_missing_vector_does_not_create_a_fake_seed():
    with pytest.raises(ValueError, match='no Qdrant point'):
        member_seed({'members':[{'points':[]}]},0)


def test_api_validation_explanation_is_preserved():
    assert api_error_detail({'detail':'requires exactly one seed point'},'422') == 'requires exactly one seed point'
    assert api_error_detail({'detail':[{'loc':['body','point_ids',0],'msg':'Invalid UUID'}]},'422') == 'point_ids.0: Invalid UUID'
    assert api_error_detail({},'HTTP 500') == 'HTTP 500'


@pytest.mark.parametrize('kwargs', [
    {'cluster_id':UUID(int=2),'neighbors':1},
    {'point_ids':[UUID(int=1)],'neighbors':1,'offset':1},
    {'point_ids':[UUID(int=1),UUID(int=2)],'neighbors':1,'offset':1},
])
def test_api_rejects_invalid_expansion_before_database_or_qdrant(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(inspect_neighborhood(None,**kwargs))


def test_cluster_expansion_first_loads_cluster_to_resolve_its_medoid():
    payload = build_inspection_request('propositions_v1', cluster, '', 1, 0, 0)
    assert payload['cluster_id'] == cluster
    assert payload['neighbors'] == 0
