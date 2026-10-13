"""Child I2V lifecycle checks using local providers; no paid calls."""
import threading
from pathlib import Path

import pytest

from src.api.task_runtime import TaskRuntimeRegistry
from src.media import shot_video_jobs as jobs


@pytest.fixture
def local(monkeypatch, tmp_path):
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(jobs, 'task_runtime', runtime, raising=False)
    monkeypatch.setattr(jobs, 'duration_options', lambda: (5,))
    monkeypatch.setattr(jobs, '_has_audio_track', lambda _: False)
    image = tmp_path / 'frame.png'
    image.write_bytes(b'frame')
    return runtime, image, tmp_path / 'jobs'


def test_parent_cancel_prevents_queued_child_first_paid_submit(local, monkeypatch):
    runtime, image, base = local
    parent = runtime.begin('queued')
    queued = []
    class Thread:
        def __init__(self, *, target, **kwargs): queued.append(target)
        def start(self): pass
    monkeypatch.setattr(jobs.threading, 'Thread', Thread)
    class Provider:
        def submit(self, *args, **kwargs): pytest.fail('cancelled queued child submitted paid request')
    jobs.start('queued', 0, image, '动作', duration=5, base=base, provider_factory=Provider, cancellation=parent)
    assert not runtime.wait_until_stopped('queued', 0)
    parent.cancel()
    queued[0]()
    runtime.finish('queued', parent)
    assert runtime.wait_until_stopped('queued', 0)
    assert jobs.status('queued', 0, base=base)['status'] == 'cancelled'


def test_shutdown_and_delete_wait_for_child_download_after_parent_exits(local):
    runtime, image, base = local
    parent = runtime.begin('downloading')
    entered, release = threading.Event(), threading.Event()
    class Provider:
        def submit(self, *args, **kwargs): return 'remote-paid-id'
        def wait(self, task): return 'https://example.invalid/video'
        def download(self, url, output):
            entered.set()
            assert release.wait(3)
            Path(output).write_bytes(b'saved video')
    jobs.start('downloading', 0, image, '动作', duration=5, base=base, provider_factory=Provider, cancellation=parent)
    assert entered.wait(2)
    parent.cancel()
    runtime.finish('downloading', parent)
    assert runtime.claim_delete('downloading')
    assert not runtime.wait_until_stopped('downloading', 0)
    runtime.close()
    assert not runtime.wait_idle(0)
    release.set()
    assert runtime.wait_until_stopped('downloading', 2)
    assert runtime.wait_idle(0)
    saved = jobs.status('downloading', 0, base=base)
    assert saved['status'] == 'completed'
    assert saved['provider_task_id'] == 'remote-paid-id'
    assert Path(saved['video_path']).read_bytes() == b'saved video'


def test_cancelled_resume_preserves_remote_id_without_resubmitting(local):
    runtime, image, base = local
    output = base / 'resume' / 'old.mp4'
    jobs._write('resume', 0, {
        'status': 'cancelled', 'image_signature': jobs._identity(image),
        'prompt': '动作', 'duration': 5, 'provider_task_id': 'already-paid',
        'video_path': str(output), 'reference_fingerprint': 'first_frame',
    }, base)
    completed = threading.Event()
    class Provider:
        def submit(self, *args, **kwargs): pytest.fail('cancelled remote task was resubmitted')
        def wait(self, task):
            assert task == 'already-paid'
            return 'https://example.invalid/video'
        def download(self, url, output):
            Path(output).write_bytes(b'recovered')
            completed.set()
    jobs.start('resume', 0, image, '动作', duration=5, base=base, provider_factory=Provider)
    assert completed.wait(2)
    assert runtime.wait_until_stopped('resume', 2)
    assert jobs.status('resume', 0, base=base)['status'] == 'completed'


def test_standalone_sibling_shots_can_queue_without_conflicting_writer(local,monkeypatch):
    runtime,image,base=local
    queued=[]
    class Thread:
        def __init__(self,*,target,**kwargs):queued.append(target)
        def start(self):pass
    monkeypatch.setattr(jobs.threading,'Thread',Thread)
    class Provider:
        def submit(self,*a,**kw):return 'paid-id'
        def wait(self,task):return 'https://example.invalid/video'
        def download(self,url,output):Path(output).write_bytes(b'video')
    try:
        jobs.start('siblings',0,image,'动作',duration=5,base=base,provider_factory=Provider)
        jobs.start('siblings',1,image,'动作',duration=5,base=base,provider_factory=Provider)
        assert len(queued)==2
        assert runtime.begin('siblings') is None  # project writers still excluded
    finally:
        for work in queued:work()
    assert runtime.wait_until_stopped('siblings',1)


def test_child_lease_excludes_other_writers_and_unrelated_parent():
    runtime=TaskRuntimeRegistry()
    parent=runtime.begin('planning')
    assert runtime.register_video_child('planning','shot-video:a') is None
    assert runtime.register_video_child('planning','shot-video:a',TaskRuntimeRegistry().begin('other')) is None
    runtime.finish('planning',parent)
    runtime.register_export('exporting','mp4:job')
    assert runtime.register_video_child('exporting','shot-video:b') is None
    runtime.finish_export('exporting','mp4:job')
    runtime.claim_delete('deleting')
    assert runtime.register_video_child('deleting','shot-video:c') is None
