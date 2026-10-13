"""Candidate reads and local recovery cannot authorize new paid work."""
import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from src.api import shot_video_routes as routes
from src.media import video_first


@pytest.fixture
def candidate(monkeypatch, tmp_path):
    image = tmp_path / 'frame.png'
    image.write_bytes(b'image')
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'mp4')
    segment = {'segment_index': 0, 'text': '原句', 'image_path': str(image), 'image_status': 'completed'}
    plan = {'status': 'failed', 'segments': ['原句'], 'shots': {'0': {'scene_id': 'room', 'motion_prompt': 'saved motion', 'duration': 5}}}
    for module in (routes, video_first):
        monkeypatch.setattr(module, 'load_plan', lambda _: plan)
        monkeypatch.setattr(module, 'lookup_shot', lambda *a: {})
        monkeypatch.setattr(module, 'compose_motion_prompt', lambda *a: 'composed motion')
        monkeypatch.setattr(module, 'compile_video_prompt', lambda task, text, prompt, refs, cfg: prompt)
    jobs = video_first.shot_video_jobs
    monkeypatch.setattr(jobs, '_video_config', lambda: {})
    monkeypatch.setattr(jobs, 'duration_options', lambda: (5,))
    monkeypatch.setattr(jobs, 'default_duration', lambda: 5)
    monkeypatch.setattr(jobs, 'video_provider', lambda: 'wan')
    snapshot = {'status': 'completed', 'prompt': 'composed motion', 'duration': 5, 'reference_fingerprint': 'scene-ref'}
    monkeypatch.setattr(jobs, 'status', lambda *a: snapshot)
    monkeypatch.setattr(jobs, 'matching_video_path', lambda *a: clip)
    monkeypatch.setattr(routes, '_segment', lambda *a: segment)
    monkeypatch.setattr(routes.h3_references, 'mode', lambda _: 'first_frame')
    def refs(task, image, scene):
        assert scene == 'room'
        return {'fingerprint': 'scene-ref'}
    monkeypatch.setattr(routes.h3_references, 'for_shot', refs)
    monkeypatch.setattr(routes.h3_references, 'fingerprint_now', lambda task, image, scene: 'scene-ref' if scene == 'room' else 'wrong-scene')
    return segment, plan, snapshot, clip


def test_rejected_candidate_saved_scene_reference_keeps_file_access_and_resume_prompt(monkeypatch, candidate):
    segment, _, snapshot, clip = candidate
    response = asyncio.run(routes.get_shot_video_file('task', 0))
    assert response.path == clip
    view = routes._view('task', 0, segment, SimpleNamespace(url_for=lambda *a, **kw: 'local/video'))
    assert view['status'] == 'completed' and view['video_url']
    snapshot.update(status='interrupted', provider_task_id='already-paid')
    called = []
    monkeypatch.setattr(routes.shot_video_jobs, 'start', lambda *a, **kw: called.append((a, kw)))
    asyncio.run(routes.generate_shot_video('task', 0, routes.CreateShotVideo(prompt='saved motion', duration=5), SimpleNamespace(url_for=lambda *a, **kw: 'local/video')))
    assert called[0][0][3] == 'composed motion'
    assert called[0][1]['reference_assets']['fingerprint'] == 'scene-ref'


def test_candidate_direction_requires_exact_narration(candidate):
    assert not routes._candidate_direction('task', 0, '修改文稿')
    assert not video_first.candidate_direction('task', 0, '修改文稿')


def test_local_readiness_requires_exact_completed_prompt_duration_reference_and_file(monkeypatch, candidate):
    segment, _, snapshot, _ = candidate
    assert video_first.local_only_ready('task', [segment], '16:9', 5)
    for key, value in [('prompt', 'other'), ('duration', 10), ('reference_fingerprint', 'other'), ('status', 'interrupted')]:
        old = snapshot[key]
        snapshot[key] = value
        assert not video_first.local_only_ready('task', [segment], '16:9', 5)
        snapshot[key] = old
    monkeypatch.setattr(video_first.shot_video_jobs, 'matching_video_path', lambda *a: None)
    assert not video_first.local_only_ready('task', [segment], '16:9', 5)


def test_settings_reject_director_runtime_before_reading_or_writing_db(monkeypatch):
    from src.api import routes as workspace
    from src.api.task_runtime import TaskRuntimeRegistry
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(workspace, 'task_runtime', runtime)
    runtime.begin('reviewing')
    monkeypatch.setattr(workspace.mysql_client, 'get_task', lambda _: pytest.fail('locked settings read DB'))
    with pytest.raises(HTTPException) as error:
        asyncio.run(workspace.update_task_workspace_settings('reviewing', {'visual_style': 'new'}))
    assert error.value.status_code == 409


def test_local_only_rechecks_before_worker_and_never_submits_on_changed_clip(monkeypatch, candidate, tmp_path):
    from src.api import task_runtime as runtime_module
    from src.api.task_runtime import TaskRuntimeRegistry
    from src.config import Config
    segment, _, snapshot, _ = candidate
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(runtime_module, 'task_runtime', runtime)
    workers = []
    monkeypatch.setattr(video_first, '_launch', lambda task, work, token, pool: workers.append(work))
    monkeypatch.setattr(video_first.shot_video_jobs, 'start', lambda *a, **kw: pytest.fail('local-only submitted paid task'))
    video_first.start('local-only', [segment], duration=5, confirmed=True, local_only=True, audio_mode='mute')
    snapshot['status'] = 'interrupted'
    workers[0]()
    assert video_first._load('local-only')['status'] == 'failed'
    assert not runtime.is_running('local-only')
