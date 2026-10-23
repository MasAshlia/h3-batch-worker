from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from h3_worker.errors import ConfigurationError
from h3_worker.review import ReviewService, read, write
from h3_worker.ui_server import UiController
import h3_worker.ui_server as ui_module


PROMPT = 'SHOT 18-1 FIRST\nSHOT DURATION: 6 seconds\n<Picture 1> = Asset R01.\n\n[PROMPT]\nOne person looks left.\n'


@pytest.fixture
def review(config, tmp_path, monkeypatch):
    assets = tmp_path / 'assets'
    assets.mkdir()
    asset = assets / 'R01.png'
    asset.write_bytes((Path(__file__).parents[2] / 'fixtures/assets/linmo.png').read_bytes())
    cfg = replace(config, output_root=tmp_path/'runs', asset_roots=(assets,), asset_index=tmp_path/'index.json')
    folder = cfg.output_root/'source'
    folder.mkdir(parents=True)
    mapping = {'asset_id': 'R01', 'path': str(asset), 'sha256': sha256(asset.read_bytes()).hexdigest()}
    write(folder/'run.json', {'episode': 'EP18', 'run_name': 'A4 E18 HMS', 'shots': [
        {'shot_id': 'S18-1', 'prompt': PROMPT, 'output': {'filename': 'old.mp4'}, 'asset_mapping': {'ref_image_0': mapping}},
        {'shot_id': 'S18-2', 'prompt': PROMPT, 'output': None, 'asset_mapping': {'ref_image_0': {**mapping, 'asset_id': 'R02'}}},
    ]})
    class Client:
        def __init__(self, *args): pass
        def get_h3_reference_limit(self): return 9
    monkeypatch.setattr(ui_module, 'RealComfyClient', Client)
    ui = UiController(cfg, workspace=tmp_path/'jobs')
    return ui.reviews


def test_draft_reopens_and_source_is_immutable(review):
    before = (review.ui.config.output_root/'source/run.json').read_bytes()
    item = review.create('source', 0)
    assert review.create('source', 0)['id'] == item['id']
    review.update(item['id'], {'prompt': PROMPT.replace('left', 'right'), 'feedback': '看右边'})
    assert (review.ui.config.output_root/'source/run.json').read_bytes() == before
    assert '-One person looks left.' in review.get(item['id'])['diff']


def test_asset_snapshot_is_isolated_and_includes_other_task_assets(review):
    item = review.create('source', 0)
    assert set(item['assets']) == {'R01', 'R02'}
    report = review.validate({**item, 'prompt': PROMPT.replace('R01', 'R02')})
    assert report['ready']


@pytest.mark.parametrize('change', [lambda p:p.replace('18-1','18-2'), lambda p:p.replace('6 seconds','7 seconds'), lambda p:p.replace('R01','NEW'), lambda p:p+PROMPT])
def test_revision_rejects_identity_duration_assets_and_multiple_shots(review, change):
    item = review.create('source', 0)
    with pytest.raises(ConfigurationError): review.validate({**item, 'prompt':change(PROMPT)})


def test_source_asset_change_is_rejected(review):
    (review.ui.config.asset_roots[0]/'R01.png').write_bytes(b'changed')
    with pytest.raises(ConfigurationError, match='changed'): review.create('source', 0)


def test_snapshot_tampering_is_rejected(review):
    item = review.create('source', 0)
    (review.ui.upload_root/item['bundle_id']/'assets/001.png').write_bytes(b'changed')
    with pytest.raises(ConfigurationError, match='snapshot changed'): review.validate(item)


@pytest.mark.parametrize('body',[{'count':0},{'count':5},{'count':True},{'screenshots':['data:image/png;base64,AAAA']},{'screenshots':['x']*5}])
def test_invalid_counts_and_images(review, body):
    item = review.create('source', 0)
    with pytest.raises(ConfigurationError): review.update(item['id'], body)


def test_ai_rewrite_preserves_feedback_and_model_history(review, monkeypatch):
    item = review.create('source', 0)
    review.settings({'base_url':'https://example.test/v1','model':'vision','api_key':'secret'})
    review.update(item['id'], {'feedback':'看右边'})
    monkeypatch.setattr(review, 'call_ai', lambda messages:json.dumps({'prompt':PROMPT.replace('left','right'),'summary':'视线朝右'}))
    result = review.rewrite(item['id'])
    assert result['preflight']['ready'] and result['turns'][0]['feedback']=='看右边'
    assert result['model']=='vision' and result['summary']=='视线朝右'
    assert 'api_key' not in review.settings()


def test_invalid_ai_response_does_not_lose_draft(review, monkeypatch):
    item = review.create('source', 0)
    review.update(item['id'], {'feedback':'修改'})
    monkeypatch.setattr(review, 'call_ai', lambda messages:'invalid')
    with pytest.raises(ConfigurationError): review.rewrite(item['id'])
    assert review.get(item['id'])['feedback']=='修改'
    assert review.get(item['id'])['prompt']==PROMPT


def test_queue_idempotency_fifo_and_no_submit_while_busy(review, monkeypatch):
    first=review.create('source',0)
    review.enqueue(first['id'])
    review.enqueue(first['id'])
    second=review.create('source',0)
    review.enqueue(second['id'])
    calls=[]
    monkeypatch.setattr(review.ui,'state',lambda:{'running':True})
    monkeypatch.setattr(review.ui,'start',lambda *a,**kw:calls.append(a) or {'session_id':'session'})
    review.tick()
    assert not calls
    monkeypatch.setattr(review.ui,'state',lambda:{'running':False})
    review.tick()
    assert len(calls)==1 and review.get(first['id'])['status']=='RUNNING'
    assert review.get(second['id'])['status']=='QUEUED'


def test_restart_never_resubmits_dispatched_work(review):
    item=review.create('source',0)
    item['status']='RUNNING'
    write(review.path(item['id']),item)
    restarted=ReviewService(review.ui)
    assert restarted.get(item['id'])['status']=='UNKNOWN_SUBMISSION'
    assert not restarted.pending()


def test_new_normal_run_cannot_jump_confirmed_revision_queue(review):
    item=review.create('source',0)
    review.enqueue(item['id'])
    with pytest.raises(ConfigurationError,match='queued'): review.ui.start('anything',{})


def test_provider_exception_is_sanitized(review, monkeypatch):
    review.settings({'base_url':'https://example.test/v1','model':'vision','api_key':'PRIVATE-KEY'})
    def fail(*a,**kw): raise RuntimeError('PRIVATE-KEY upstream body')
    monkeypatch.setattr('h3_worker.review.urlopen',fail)
    with pytest.raises(ConfigurationError) as error: review.call_ai([])
    assert 'PRIVATE-KEY' not in str(error.value.to_dict())


def test_valid_screenshot_persists_and_queued_draft_survives_restart(review):
    import base64
    item=review.create('source',0)
    png=base64.b64encode((review.ui.config.asset_roots[0]/'R01.png').read_bytes()).decode()
    review.update(item['id'],{'screenshots':['data:image/png;base64,'+png]})
    review.enqueue(item['id'])
    restarted=ReviewService(review.ui)
    assert restarted.get(item['id'])['status']=='QUEUED'
    assert restarted.get(item['id'])['screenshots'][0].endswith(png)


def test_failed_preflight_never_dispatches(review, monkeypatch):
    item=review.create('source',0)
    review.update(item['id'],{'prompt':PROMPT.replace('R01','UNKNOWN')})
    calls=[]
    monkeypatch.setattr(review.ui,'start',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(ConfigurationError): review.enqueue(item['id'])
    assert not calls and review.get(item['id'])['status']=='DRAFT'


def test_http_review_routes_and_private_settings(review):
    from http.server import ThreadingHTTPServer
    from h3_worker.ui_server import UiHandler
    import threading
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    handler=type('TestHandler',(UiHandler,),{'controller':review.ui,'html_path':Path(ui_module.__file__).with_name('ui.html')})
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def request(path,body=None,origin=None):
        headers={'Content-Type':'application/json'}
        if origin: headers['Origin']=origin
        req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with urlopen(req) as response:return response.read()
    try:
        assert b'Compact video review' in request('/review.js')
        request('/api/ai/settings',{'base_url':'https://example.test/v1','model':'vision','api_key':'secret'})
        assert b'secret' not in request('/api/ai/settings')
        item=json.loads(request('/api/reviews',{'batch_id':'source','shot_index':0}))
        assert json.loads(request('/api/reviews/'+item['id']))['status']=='DRAFT'
        assert json.loads(request('/api/reviews'))['reviews'][0]['id']==item['id']
        with pytest.raises(HTTPError): request('/api/ai/settings',{},'https://untrusted.test')
    finally:
        server.shutdown()
        server.server_close()
