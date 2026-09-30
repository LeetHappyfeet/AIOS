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
    with pytest.raises(gr.Error,match='Select exactly one seed'):
        ui.inspect('propositions_v1','',seed,1,0,0)


def test_missing_seed_fails_locally_without_http(monkeypatch):
    def unexpected(*args,**kwargs):
        pytest.fail('Invalid selection must not send an HTTP request')
    monkeypatch.setattr(ui.requests,'post',unexpected)
    with pytest.raises(gr.Error,match='click one member row'):
        ui.inspect('propositions_v1',cluster,'',1,0,0)


def test_row_selection_and_inspect_callbacks_are_wired():
    with gr.Blocks() as app:
        ui.render_semantic_inspector()
    functions = {dependency['api_name'] for dependency in app.config['dependencies']}
    assert 'select_seed' in functions
    assert 'inspect' in functions
