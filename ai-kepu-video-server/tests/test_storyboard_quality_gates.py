"""Review candidates cannot fund new media; existing paid jobs remain recoverable."""
from pathlib import Path

import pytest

from src.api.task_runtime import TaskRuntimeRegistry
from src.media import shot_video_jobs as jobs
from src.media.image_generator import ImageGenerator
from src.text import storyboard_director as director


@pytest.fixture
def local(monkeypatch, tmp_path):
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(jobs, 'task_runtime', runtime)
    monkeypatch.setattr(jobs, 'duration_options', lambda: (5,))
    monkeypatch.setattr(jobs, '_has_audio_track', lambda _: False)
    monkeypatch.setattr(jobs, 'video_provider', lambda: 'wan')
    queued = []
    class Thread:
        def __init__(self, *, target, **kwargs): queued.append(target)
        def start(self): pass
    monkeypatch.setattr(jobs.threading, 'Thread', Thread)
    image = tmp_path / 'frame.png'
    image.write_bytes(b'frame')
    plan = {'review_required': True, 'status': 'reviewing'}
    monkeypatch.setattr(director, 'load_plan', lambda _: plan)
    return runtime, image, tmp_path / 'jobs', queued, plan


class Provider:
    def submit(self, *args, **kwargs): pytest.fail('new paid submission forbidden')
    def wait(self, task):
        assert task == 'paid'
        return 'https://example.invalid/video'
    def download(self, url, output): Path(output).write_bytes(b'recovered')


@pytest.mark.parametrize('state', ['reviewing', 'changed_digest'])
def test_new_video_rejected_before_job_creation(local, state):
    runtime, image, base, queued, plan = local
    if state == 'changed_digest':
        plan.update(status='completed', quality={'version': 1, 'status': 'passed', 'candidate_digest': 'stale'})
    with pytest.raises(ValueError, match='导演'):
        jobs.start('new', 0, image, '动作', duration=5, base=base, provider_factory=Provider)
    assert not queued
    assert runtime.wait_idle(0)


def test_queued_video_rechecks_candidate(local):
    runtime, image, base, queued, plan = local
    plan.clear()
    jobs.start('queued-quality', 0, image, '动作', duration=5, base=base, provider_factory=Provider)
    plan.update(review_required=True, status='repairing')
    queued.pop()()
    assert jobs.status('queued-quality', 0, base=base)['status'] == 'failed'
    assert runtime.wait_idle(0)


@pytest.mark.parametrize('state', ['cancelled', 'completed'])
def test_existing_video_reusable_despite_rejected_plan(local, state):
    runtime, image, base, queued, plan = local
    output = base / 'old.mp4'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'existing')
    jobs._write('recover', 0, {'status': state, 'image_signature': jobs._identity(image),
        'prompt': '动作', 'duration': 5, 'provider_task_id': 'paid',
        'video_path': str(output), 'reference_fingerprint': 'first_frame'}, base)
    result = jobs.start('recover', 0, image, '动作', duration=5, base=base, provider_factory=Provider)
    if state == 'completed':
        assert result['video_path'] == str(output)
        assert not queued
    else:
        queued.pop()()
        assert jobs.status('recover', 0, base=base)['status'] == 'completed'
    assert runtime.wait_idle(0)


def test_image_rejected_before_provider_or_segment_lookup(local, monkeypatch, tmp_path):
    monkeypatch.setattr('src.database.mysql_client.get_segments', lambda _: pytest.fail('must gate before segment lookup'))
    generator = object.__new__(ImageGenerator)
    generator.project_task_id = 'image-quality'
    with pytest.raises(ValueError, match='导演'):
        generator.generate('A clear scene')


@pytest.mark.parametrize('stale', [False, True])
def test_native_existing_clips_reuse_blocked_candidate_only_with_matching_narration(monkeypatch, tmp_path, stale):
    from src.config import Config
    from src.media import video_first as vf
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    image = tmp_path / 'image.png'
    image.write_bytes(b'image')
    segment = {'segment_index': 0, 'text': '原文', 'image_path': str(image), 'image_status': 'completed'}
    plan = {'review_required': True, 'status': 'failed', 'segments': ['旧原文' if stale else '原文'],
            'shots': {'0': {'motion_prompt': '已有准确动作', 'duration': 8}}}
    monkeypatch.setattr(vf, 'load_plan', lambda _: plan)
    monkeypatch.setattr(vf, 'lookup_shot', lambda *a: {})
    monkeypatch.setattr(vf.h3_references, 'mode', lambda _: 'first_frame')
    monkeypatch.setattr(vf.h3_references, 'for_shot', lambda *a: None)
    monkeypatch.setattr(vf, 'compose_motion_prompt', lambda p, i, prompt: prompt)
    monkeypatch.setattr(vf, 'compile_video_prompt', lambda task, text, prompt, *a: prompt)
    monkeypatch.setattr(jobs, 'duration_options', lambda: (5, 8))
    monkeypatch.setattr(jobs, 'video_provider', lambda: 'minimax')
    monkeypatch.setattr(jobs, '_video_config', lambda: {'model': 'MiniMax-H3'})
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    current = {'status': 'completed', 'image_signature': jobs._identity(image),
               'prompt': '已有准确动作', 'duration': 8, 'reference_fingerprint': 'first_frame', 'video_path': str(clip)}
    monkeypatch.setattr(jobs, 'status', lambda *a: current)
    monkeypatch.setattr(jobs, 'matching_video_path', lambda *a: str(clip))
    monkeypatch.setattr(jobs, 'start', lambda *a, **kw: pytest.fail('must never dispatch new paid job'))
    # A completed whole-video cache exercises start's matching path without a thread.
    shots = vf._shot_inputs('reuse-quality', [segment], '16:9', 5)
    if stale:
        with pytest.raises(vf.VideoFirstError, match='导演'):
            vf.start('reuse-quality', [segment], confirmed=True, duration=5, audio_mode='mute', subtitles=False)
    else:
        assert shots[0]['motion_prompt'] == current['prompt']
        assert shots[0]['duration'] == 8
        output = tmp_path / 'final.mp4'
        output.write_bytes(b'final')
        monkeypatch.setattr(vf, '_load', lambda _: {'fingerprint': vf._fingerprint(shots, '16:9', 'mute'),
            'status': 'completed', 'output_path': str(output), 'subtitles': False, 'max_duration': None})
        monkeypatch.setattr(vf, '_current', lambda *a: {'status': 'completed'})
        assert vf.start('reuse-quality', [segment], confirmed=True, duration=5, audio_mode='mute', subtitles=False)['status'] == 'completed'
        from src.api import task_runtime as runtime_module
        runtime = TaskRuntimeRegistry()
        monkeypatch.setattr(runtime_module, 'task_runtime', runtime)
        original_load = lambda task: __import__('json').loads(vf._record_path(task).read_text()) if vf._record_path(task).exists() else {}
        monkeypatch.setattr(vf, '_load', original_load)
        monkeypatch.setattr(vf, '_launch', lambda task, work, token, pool: work())
        monkeypatch.setattr(vf.h3_references, 'fingerprint_now', lambda *a: 'first_frame')
        monkeypatch.setattr(vf, '_has_audio', lambda _: False)
        rendered = []
        def render(paths, destination, **kwargs):
            rendered.extend(paths)
            Path(destination).write_bytes(b'local final')
            return str(destination)
        monkeypatch.setattr(vf, 'render_clips', render)
        vf.start('reuse-quality', [segment], confirmed=True, duration=5, audio_mode='mute', subtitles=False)
        assert rendered == [str(clip)]
        assert original_load('reuse-quality')['status'] == 'completed'
        assert runtime.wait_idle(0)


@pytest.mark.parametrize('mismatch', ['prompt', 'narration'])
def test_image_dispatch_requires_reviewed_prompt_and_narration(monkeypatch,mismatch):
    p={'review_required':True,'status':'completed','segments':['旁白'],
       'visual_bible':{},'shots':{'0':{'image_prompt':'A blue sphere'}},
       'quality':{'version':director._QUALITY_VERSION,'status':'passed'}}
    p['quality']['candidate_digest']=director._candidate_digest(p)
    monkeypatch.setattr(director,'load_plan',lambda _:p)
    monkeypatch.setattr('src.database.mysql_client.get_segments',lambda _:[{'segment_index':0,'text':'修改旁白' if mismatch=='narration' else '旁白'}])
    g=object.__new__(ImageGenerator);g.project_task_id='changed'
    with pytest.raises(ValueError,match='审查'):
        g.generate('A red cube' if mismatch=='prompt' else 'A blue sphere')
