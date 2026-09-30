from types import SimpleNamespace
from uuid import UUID

import pytest

gr = pytest.importorskip('gradio')
from aios_app.ui.tabs import semantic_inspector as ui


seed = str(UUID(int=1))
cluster = str(UUID(int=2))


def test_ui_sends_single_seed_instead_of_previous_cluster(monkeypatch):
    sent = []
    def post(url, *, json, timeout):
        sent.append(json)
        return SimpleNamespace(ok=True, json=lambda: {
            'representatives':{'medoid_id':seed,'representative_ids':[seed]},
            'members':[{'proposition_id':seed,'canonical_text':'Window is open',
                        'polarity':1,'evidence':[]}], 'edges':[]})
    monkeypatch.setattr(ui.requests,'post',post)
    rows, edges, details = ui.inspect('propositions_v1',cluster,seed,1,0,0)
    assert sent[0]['point_ids'] == [seed]
    assert 'cluster_id' not in sent[0]
    assert rows[0][2] == 'Window is open'


def test_422_body_is_shown_as_gradio_error(monkeypatch):
    monkeypatch.setattr(ui.requests,'post',lambda *args,**kwargs: SimpleNamespace(
        ok=False,status_code=422,json=lambda: {'detail':'Select exactly one seed'}))
    rows, edges, details = ui.inspect('propositions_v1','',seed,1,0,0)
    assert rows == edges == []
    assert details['error'] == 'Select exactly one seed'


def test_missing_seed_fails_locally_without_http(monkeypatch):
    def unexpected(*args,**kwargs):
        pytest.fail('Invalid selection must not send an HTTP request')
    monkeypatch.setattr(ui.requests,'post',unexpected)
    rows, edges, details = ui.inspect('propositions_v1','','',1,0,0)
    assert rows == edges == []
    assert 'click one member row' in details['error']


def test_row_selection_and_inspect_callbacks_are_wired():
    with gr.Blocks() as app:
        ui.render_semantic_inspector()
    functions = {dependency['api_name'] for dependency in app.config['dependencies']}
    assert 'select_seed' in functions
    assert 'inspect' in functions


@pytest.mark.parametrize('copied_id', [False, True])
def test_cluster_can_expand_directly_using_its_medoid(monkeypatch,copied_id):
    calls = []
    def api(path,payload):
        calls.append(payload)
        return {'collection':'propositions_v1',
                'representatives':{'medoid_id':seed,'representative_ids':[seed]},
                'members':[{'proposition_id':seed,'canonical_text':'Window open','polarity':1,
                            'points':[{'point_id':seed}],'evidence':[]}],'edges':[]}
    monkeypatch.setattr(ui,'_api',api)
    listed = {'clusters':[{'cluster_id':cluster}]}
    rows, edges, details = ui.inspect('propositions_v1','' if copied_id else cluster,
                                      cluster if copied_id else '',1,0,0,listed)
    assert calls[0]['cluster_id'] == cluster and calls[0]['neighbors'] == 0
    assert calls[1]['point_ids'] == [seed] and calls[1]['neighbors'] == 1
    assert details['seed_selection']['point_id'] == seed


def test_cluster_picker_populates_choices_and_fields(monkeypatch):
    monkeypatch.setattr(ui,'_api',lambda path: {'clusters':[{'cluster_id':cluster,'member_count':3}]})
    details, dropdown = ui.list_clusters()
    assert dropdown['choices'][0][1] == cluster
    assert ui.choose_cluster(cluster) == (cluster,'','propositions_v1',0)
