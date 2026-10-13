"""Isolated route checks: fake DB/runtime/pool; no provider requests."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api import continuity_routes as routes
from src.api.task_runtime import TaskRuntimeRegistry


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    segment = {'segment_index': 0, 'text': '原句', 'image_path': str(tmp_path / 'image.png')}
    (tmp_path / 'image.png').write_bytes(b'image')
    plan = {'status': 'failed', 'segments': ['原句'], 'shots': {'0': {'index': 0, 'camera': '固定机位', 'motion_prompt': '不可提交'}}, 'quality': {'version': 1, 'status': 'needs_revision', 'issues': [{'index': 0, 'severity': 'blocking', 'code': 'mechanism', 'evidence': '因果缺失', 'required_change': '画出机制'}]}}
    updates, queued, creates = [], [], []
    monkeypatch.setattr(routes.mysql_client, 'sync_director_image_prompts', lambda *args, **kwargs: 1)
    monkeypatch.setattr(routes.mysql_client, 'get_task', lambda _: {'script_text': '原句'})
    monkeypatch.setattr(routes.mysql_client, 'get_segments', lambda _: [segment])
    monkeypatch.setattr(routes.mysql_client, 'get_active_task_operation', lambda _: None)
    monkeypatch.setattr(routes.mysql_client, 'production_list', lambda **_: [])
    monkeypatch.setattr(routes.mysql_client, 'create_task_operation', lambda **kw: creates.append(kw) or {'outcome': 'created', 'operation': {'operation_id': 'op'}})
    monkeypatch.setattr(routes.mysql_client, 'update_task_operation', lambda _, **kw: updates.append(kw))
    monkeypatch.setattr(routes, 'load_plan', lambda _: plan)
    monkeypatch.setattr(routes, 'project_ip', lambda _: None)
    monkeypatch.setattr(routes.h3_references, 'overview', lambda _: {'voice_ready': False})
    monkeypatch.setattr(routes.video_first, 'status', lambda *a, **kw: {'status': 'idle'})
    monkeypatch.setattr(routes.shot_video_jobs, 'status', lambda *a: {'status': 'idle'})
    monkeypatch.setattr(routes, 'task_runtime', TaskRuntimeRegistry())
    monkeypatch.setattr(routes, 'ArticleGenerator', lambda: object())
    monkeypatch.setattr(routes, 'generation_pool', SimpleNamespace(submit=lambda f, *a: queued.append((f, a)) or SimpleNamespace(cancel=lambda: False)))
    return plan, segment, updates, queued, creates


def test_blocked_report_retains_candidate_and_issues_without_compiling(monkeypatch, isolated):
    monkeypatch.setattr(routes, 'compile_video_prompt', lambda *a: pytest.fail('blocked prompt compiled'))
    report = routes._report('task')
    assert report['generation_blocked']
    assert report['shots'][0]['camera'] == '固定机位'
    assert report['shots'][0]['image_ready']
    assert report['shots'][0]['model_prompt'] == ''
    assert report['quality']['issues'][0]['code'] == 'mechanism'


def test_review_requires_cost_confirmation_and_dispatches_cancellable_worker(monkeypatch, isolated):
    _, segment, updates, queued, creates = isolated
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.review_director('task', routes.DirectorReviewRequest()))
    assert error.value.status_code == 400
    assert not queued
    result = asyncio.run(routes.review_director('task', routes.DirectorReviewRequest(confirm_cost=True)))
    assert result['status'] == 'reviewing'
    assert creates[0]['kind'] == 'director_review'
    callbacks = []
    def review(*a, **kw):
        callbacks.append(kw['cancel_check'])
        kw['cancel_check']()
        return {'status': 'completed', 'quality': {'version': 1, 'status': 'passed'}}
    monkeypatch.setattr(routes, 'review_existing_plan', review)
    queued[0][0](*queued[0][1])
    assert callbacks and updates[-1]['state'] == 'completed'
    assert segment['image_path']
    assert not routes.task_runtime.is_running('task')


@pytest.mark.parametrize('blocker', ['production', 'video', 'stale', 'runtime'])
def test_review_rejects_overlap_and_stale_candidates(monkeypatch, isolated, blocker):
    plan, _, _, queued, _ = isolated
    if blocker == 'production':
        monkeypatch.setattr(routes.mysql_client, 'production_list', lambda **_: [{}])
    elif blocker == 'video':
        monkeypatch.setattr(routes.video_first, 'status', lambda *a, **kw: {'status': 'verifying'})
    elif blocker == 'stale':
        plan['segments'] = ['旧句']
    else:
        routes.task_runtime.begin('task')
    with pytest.raises(HTTPException) as error:
        routes._start_replan('task', True)
    assert error.value.status_code == 409
    assert not queued


def test_cancelled_review_releases_runtime_and_keeps_plan(isolated):
    plan, _, updates, queued, _ = isolated
    routes._start_replan('task', True)
    routes.task_runtime.request_cancel('task')
    queued[0][0](*queued[0][1])
    assert updates[-1]['state'] == 'cancelled'
    assert plan['shots']['0']['motion_prompt'] == '不可提交'
    assert not routes.task_runtime.is_running('task')


def test_modified_passed_candidate_reports_needs_review(isolated):
    plan, _, _, _, _ = isolated
    plan.update(status='completed', review_required=True)
    plan['quality'].update(status='passed', candidate_digest='old-hash')
    report = routes._report('task')
    assert report['generation_blocked']
    assert report['quality']['status'] == 'needs_review'
    assert plan['quality']['status'] == 'passed'  # Display does not mutate the checkpoint.
    assert report['shots'][0]['model_prompt'] == ''


@pytest.mark.parametrize('review,expected', [(True, None), (False, '自定义视觉描述')])
def test_worker_preserves_review_style_context_and_parses_new_replan_style(monkeypatch, isolated, review, expected):
    _, _, _, _, _ = isolated
    captured = []
    def model(*args, **kwargs):
        captured.append(kwargs)
        return {'status': 'completed'}
    monkeypatch.setattr(routes, 'review_existing_plan', model)
    monkeypatch.setattr(routes, 'plan_storyboard', model)
    monkeypatch.setattr(routes.shot_video_jobs, 'video_provider', lambda: 'wan')
    token = routes.task_runtime.begin('style')
    routes._replan_worker('style', 'op', token, {'style': '温暖|动画|自定义视觉描述'}, ['原句'], review)
    assert captured[0]['style'] == expected


@pytest.mark.parametrize('stage', ['planning', 'reviewing', 'repairing'])
def test_orphan_director_stage_allows_explicit_retry_without_automatic_calls(isolated, stage):
    plan, _, _, queued, _ = isolated
    plan['status'] = stage
    report = routes._report('task')
    assert report['status'] == 'interrupted'
    assert report['generation_blocked']
    assert report['can_review'] and report['can_replan']
    assert not queued


def test_active_review_remains_running_and_locked(monkeypatch, isolated):
    plan, _, _, queued, _ = isolated
    plan['status'] = 'reviewing'
    monkeypatch.setattr(routes.mysql_client, 'get_active_task_operation', lambda _: {'kind': 'director_review', 'operation_id': 'op', 'state': 'running'})
    report = routes._report('task')
    assert report['status'] == 'reviewing'
    assert not report['can_review'] and not report['can_replan']
    assert not queued


def test_replan_preserves_original_brief(monkeypatch, isolated):
    captured = []
    monkeypatch.setattr(routes, 'plan_storyboard', lambda *args, **kwargs: captured.append(kwargs) or {'status': 'completed'})
    monkeypatch.setattr(routes.shot_video_jobs, 'video_provider', lambda: 'wan')
    token = routes.task_runtime.begin('brief')
    routes._replan_worker('brief', 'op', token, {'original_input': '解释月相，不要魔法', 'theme': '月亮'}, ['原句'])
    assert captured[0]['brief'] == '解释月相，不要魔法'


@pytest.mark.parametrize('outcome', [None, -1])
def test_approved_candidate_sync_failure_blocks_delivery_and_preserves_candidate(monkeypatch, isolated, outcome):
    plan, _, updates, _, _ = isolated
    plan.update(status='completed', quality=None)
    preserved = []
    monkeypatch.setattr(routes, 'review_existing_plan', lambda *args, **kwargs: plan)
    monkeypatch.setattr(routes.mysql_client, 'sync_director_image_prompts', lambda *args, **kwargs: outcome)
    def failed(task_id, **kwargs):
        preserved.append(kwargs)
        plan['status'] = 'failed'
    monkeypatch.setattr(routes, 'record_planning_failure', failed)
    token = routes.task_runtime.begin('sync')
    routes._replan_worker('sync', 'op', token, {'plan_version': 7}, ['原句'], True)
    assert preserved[0]['stage'] == 'synchronization'
    assert plan['shots']['0']['motion_prompt'] == '不可提交'
    assert updates[-1]['state'] == 'failed'


def test_active_review_displays_persisted_repair_stage(monkeypatch, isolated):
    plan, _, _, _, _ = isolated
    plan['status'] = 'repairing'
    monkeypatch.setattr(routes.mysql_client, 'get_active_task_operation', lambda _: {'kind': 'director_review', 'state': 'running'})
    report = routes._report('task')
    assert report['status'] == 'repairing'
    assert not report['can_review']


def test_blocked_report_displays_sanitized_safe_provider_reason(isolated):
    plan, _, _, _, _ = isolated
    plan['error'] = {'safe_message': '服务商要求付费，请检查账号余额或计费状态后重试。'}
    assert '服务商要求付费' in routes._report('task')['error']
