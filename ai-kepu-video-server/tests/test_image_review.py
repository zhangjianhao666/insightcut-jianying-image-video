import json

import pytest
from fastapi import HTTPException

from src.media import image_review as review
from src.api import image_review_routes as routes


def response(verdict='pass'):
    checks = [{'criterion': k, 'verdict': verdict, 'evidence': 'Visible evidence',
               'repair_target': 'image'} for k in review.CRITERIA]
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'checks': checks})}}]}


def test_real_visual_verdict_schema_and_missing_identity():
    current = {'contract': {'character_required': True}, 'references': {}}
    verdict, checks = review.parse_response(response(), current)
    assert verdict == 'unknown'
    assert next(r for r in checks if r['criterion'] == 'character')['repair_target'] == 'reference'
    assert review.parse_response(response('fail'), current)[0] == 'fail'


def test_truncated_duplicate_and_unsafe_output_unknown():
    current = {'contract': {}, 'references': {}}
    data = response()
    data['choices'][0]['finish_reason'] = 'length'
    assert review.parse_response(data, current) == ('unknown', [])
    data = response()
    rows = json.loads(data['choices'][0]['message']['content'])
    rows['checks'][0]['evidence'] = 'Bearer secret'
    data['choices'][0]['message']['content'] = json.dumps(rows)
    assert review.parse_response(data, current) == ('unknown', [])


def test_path_gating_and_explicit_confirmation():
    with pytest.raises(review.ReviewError):
        review.location('../escape', 0)
    with pytest.raises(HTTPException) as error:
        routes._start('task', 0, routes.ReviewRequest())
    assert error.value.status_code == 400


def test_stale_does_not_reuse_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(review.Config, 'BASE_DIR', tmp_path)
    review.save('task', 0, {'image_hash': 'old', 'contract_hash': 'contract', 'status': 'completed', 'verdict': 'pass'})
    monkeypatch.setattr(review, 'snapshot', lambda *a: {'image_hash': 'new', 'contract_hash': 'contract', 'contract': {}})
    result = review.read('task', 0)
    assert result['stale'] and result['verdict'] == 'unknown'


def test_safe_worker_failure_releases_runtime(monkeypatch):
    from src.api.task_runtime import TaskRuntimeRegistry
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(routes, 'task_runtime', runtime)
    saved = []
    monkeypatch.setattr(review, 'save', lambda *args: saved.append(args[-1].copy()))
    def fail(*args):
        raise RuntimeError('Bearer confidential')
    monkeypatch.setattr(review, 'inspect', fail)
    token = runtime.begin('task')
    routes._worker('task', 0, token, {'image_hash': 'i', 'contract_hash': 'c'}, 'qwen3-vl-plus')
    assert saved[-1]['status'] == 'failed'
    assert 'confidential' not in json.dumps(saved)
    assert not runtime.is_running('task')


def test_fake_transport_official_endpoint_no_redirect_or_retry(monkeypatch):
    monkeypatch.setattr(review.Config, 'image_config', lambda: {'provider': 'wan', 'api_key': 'test-only', 'base_url': 'https://dashscope.aliyuncs.com/api/v1'})
    calls = []
    class Transport:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            class Reply:
                status_code = 200
                content = b'{}'
                def json(self):
                    return response('fail')
            return Reply()
    verdict, _ = review.inspect({'contract': {}, 'references': {}, 'image': 'data:image/jpeg;base64,AA', 'reference_images': []}, 'qwen3-vl-plus', Transport())
    assert verdict == 'fail' and len(calls) == 1
    assert calls[0][0] == 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['timeout'] == (10, 90)
    assert calls[0][1]['json']['enable_thinking'] is False


def test_style_and_required_scene_are_critical():
    current = {'contract': {'reference_policy': {'scene': True}}, 'references': {'scene': 'hash'}}
    data = response('pass')
    rows = json.loads(data['choices'][0]['message']['content'])
    for row in rows['checks']:
        if row['criterion'] in ('style', 'scene'):
            row['verdict'] = 'not_applicable'
    data['choices'][0]['message']['content'] = json.dumps(rows)
    verdict, checks = review.parse_response(data, current)
    assert verdict == 'unknown'
    assert all(r['verdict'] == 'unknown' for r in checks if r['criterion'] in ('style', 'scene'))
    rows['checks'][3]['verdict'] = 'fail'
    data['choices'][0]['message']['content'] = json.dumps(rows)
    assert review.parse_response(data, current)[0] == 'fail'
    current['references'] = {}
    assert next(r for r in review.parse_response(response(), current)[1] if r['criterion'] == 'scene')['verdict'] == 'unknown'


def test_object_snapshot_never_serializes_unused_portrait(tmp_path, monkeypatch):
    from src.text import image_contract
    from src.media import h3_references
    monkeypatch.setattr(review.Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(review.mysql_client, 'get_task', lambda *a: {'task_id': 'task'})
    monkeypatch.setattr(review.mysql_client, 'get_segments', lambda *a: [{'segment_index': 0, 'image_path': 'image', 'text': 'text'}])
    monkeypatch.setattr(review, 'load_plan', lambda *a: {})
    monkeypatch.setattr(review, 'lookup_shot', lambda *a: {})
    monkeypatch.setattr(image_contract, 'compile_shot', lambda *a, **kw: {'subject_mode': 'object', 'reference_policy': {'character': False, 'scene': False}})
    monkeypatch.setattr(review, 'project_ip', lambda *a: {'reference_image': 'portrait'})
    monkeypatch.setattr(h3_references, '_read', lambda *a: {})
    paths = []
    def serialize(path):
        paths.append(path)
        return 'hash', 'data:image/jpeg;base64,AA'
    monkeypatch.setattr(review, 'picture', serialize)
    current = review.snapshot('task', 0)
    assert paths == ['image'] and current['reference_images'] == []
    assert next(r for r in review.parse_response(response(), current)[1] if r['criterion'] == 'scene')['verdict'] == 'not_applicable'


def test_invalid_config_rejected_before_snapshot_or_queue(monkeypatch):
    monkeypatch.setattr(review.Config, 'image_config', lambda: {'provider': 'wan', 'api_key': 'test-only', 'base_url': 'http://127.0.0.1'})
    def forbidden(*args):
        pytest.fail('snapshot must not run with invalid configuration')
    monkeypatch.setattr(review, 'snapshot', forbidden)
    with pytest.raises(HTTPException) as error:
        routes._start('task', 0, routes.ReviewRequest(confirm_model_call=True))
    assert error.value.status_code == 400


def test_read_without_result_is_not_reviewed(tmp_path, monkeypatch):
    monkeypatch.setattr(review.Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(review, 'snapshot', lambda *a: {'image_hash': 'hash', 'contract': {}})
    assert review.read('task', 0)['status'] == 'not_reviewed'


def test_safe_receipt_filters_payload_and_unsafe_fields():
    data = {'id': 'chatcmpl-example', 'model': 'qwen3-vl-plus', 'messages': 'secret',
            'usage': {'prompt_tokens': 42, 'completion_tokens': 10, 'total_tokens': 52,
                      'secret': 'key', 'prompt_tokens_details': {'image_tokens': 30},
                      'completion_tokens_details': {'reasoning_tokens': 0}}}
    receipt = review.safe_receipt(data, 'qwen3-vl-plus')
    assert receipt == {'model': 'qwen3-vl-plus', 'request_id': 'chatcmpl-example',
                       'response_model': 'qwen3-vl-plus', 'usage': {'prompt_tokens': 42, 'completion_tokens': 10,
                       'total_tokens': 52, 'image_tokens': 30, 'reasoning_tokens': 0}}
    assert review.safe_receipt({'id': 'sk-secret', 'usage': {'total_tokens': True, 'completion_tokens': -1}}, 'qwen3-vl-plus')['usage'] == {}


def test_saved_reviewer_version_change_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(review.Config, 'BASE_DIR', tmp_path)
    current = {'image_hash': 'hash', 'contract_hash': 'contract', 'contract': {}}
    monkeypatch.setattr(review, 'snapshot', lambda *a: current)
    review.save('task', 0, {**current, 'reviewer_version': 'obsolete', 'status': 'completed', 'verdict': 'pass'})
    result = review.read('task', 0)
    assert result['stale'] and result['status'] == 'not_reviewed' and result['verdict'] == 'unknown'


def test_worker_persists_only_safe_receipt(monkeypatch):
    from src.api.task_runtime import TaskRuntimeRegistry
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(routes, 'task_runtime', runtime)
    current = {'image_hash': 'i', 'contract_hash': 'c'}
    saved = []
    monkeypatch.setattr(review, 'save', lambda *a: saved.append(a[-1].copy()))
    monkeypatch.setattr(review, 'snapshot', lambda *a: current)
    def inspect(snapshot, model):
        snapshot['receipt'] = review.safe_receipt({'id': 'request-1', 'usage': {'total_tokens': 20}, 'secret': 'never-store'}, model)
        return 'unknown', []
    monkeypatch.setattr(review, 'inspect', inspect)
    routes._worker('task', 0, runtime.begin('task'), current, 'qwen3-vl-plus')
    assert saved[-1]['receipt']['usage']['total_tokens'] == 20
    assert 'never-store' not in json.dumps(saved)
    assert saved[-1]['verdict'] == 'unknown'
