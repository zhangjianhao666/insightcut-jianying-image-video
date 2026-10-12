"""Voice identity versions and native dialogue contracts; no paid requests."""
import importlib
import asyncio
import io
import wave
from pathlib import Path

import pytest

from src.config import Config
from src.media import h3_references, video_first, shot_video_jobs
from src.text import ip_library


def wav(seconds=3, sample=0):
    out = io.BytesIO()
    with wave.open(out, 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(bytes([sample, 0]) * (16000 * seconds))
    return out.getvalue()


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    return tmp_path


def test_ip_voice_versions_are_private_and_pinned_across_projects(local):
    ip = ip_library.save_profile({'name': 'Vivian'})
    first = ip_library.save_voice_reference(ip['ip_id'], wav())
    ip_library.bind_project('first', ip['ip_id'])
    edited = ip_library.save_profile({'name': 'Vivian', 'voice_style': '亲切'}, ip['ip_id'])
    assert edited['reference_voice'] == first['reference_voice']
    second = ip_library.save_voice_reference(ip['ip_id'], wav(sample=1))
    ip_library.bind_project('second', ip['ip_id'])
    assert first['reference_voice'] != second['reference_voice']
    assert ip_library.project_ip('first')['reference_voice'] == first['reference_voice']
    assert ip_library.project_ip('second')['reference_voice'] == second['reference_voice']
    assert first['voice_reference_version'] == 1
    assert second['voice_reference_version'] == 2
    assert Path(first['reference_voice']).is_file()
    assert '_private' in first['reference_voice']
    assert 'reference_voice' not in ip_library.public_profile(first)
    assert ip_library.public_profile(first)['voice_reference_ready']
    ip_library.clear_voice_reference(ip['ip_id'])
    assert Path(second['reference_voice']).is_file()
    assert ip_library.project_ip('first')['reference_voice'] == first['reference_voice']


def test_project_override_and_clear_restore_pinned_ip_voice(local):
    ip = ip_library.save_profile({'name': 'Vivian'})
    pinned = ip_library.save_voice_reference(ip['ip_id'], wav())
    ip_library.bind_project('episode', ip['ip_id'])
    frame = local / 'frame.png'
    frame.write_bytes(b'frame')
    h3_references.select_mode('episode', 'reference')
    first = h3_references.for_shot('episode', frame, '')
    assert first['audio'] == pinned['reference_voice']
    assert h3_references.overview('episode')['voice_source'] == 'ip'
    h3_references.save_voice_sample('episode', wav(sample=2))
    override = h3_references.for_shot('episode', frame, '')
    assert override['audio'] != first['audio']
    assert override['fingerprint'] != first['fingerprint']
    assert h3_references.overview('episode')['voice_source'] == 'project'
    h3_references.clear_voice('episode')
    assert h3_references.for_shot('episode', frame, '')['audio'] == first['audio']
    Path(first['audio']).unlink()
    with pytest.raises(h3_references.H3ReferenceError, match='音色'):
        h3_references.for_shot('episode', frame, '')


def test_ip_rejects_invalid_and_truncated_wav_before_version_change(local):
    ip = ip_library.save_profile({'name': 'Vivian'})
    for data in (b'not-a-wav', wav(1), wav()[:-100]):
        with pytest.raises(ValueError):
            ip_library.save_voice_reference(ip['ip_id'], data)
    assert ip_library.get_profile(ip['ip_id'])['version'] == ip['version']


def test_native_dialogue_preserves_script_and_binds_reference_before_visual_prose(local):
    module = importlib.import_module('src.text.native_dialogue')
    script = '煮熟后能做沙拉，也能打成鹰嘴豆泥。'
    result = module.compile_native_dialogue(
        '自然动作；' * 900, script,
        profile={'name': 'Vivian', 'voice_style': '温暖女声'}, has_voice_reference=True,
    )
    assert len(result) <= 1800
    assert f'<d>[Chinese] {script}</d>' in result
    assert 'Vivian (S1)' in result and '<Audio 1>' in result
    assert result.index(script) < result.index('自然动作')
    with pytest.raises(ValueError, match='台词'):
        module.compile_native_dialogue('动作', '长' * 1800)


def test_h3_whole_shot_prompt_contains_actual_segment_text(local, monkeypatch):
    frame = local / 'frame.png'
    frame.write_bytes(b'frame')
    monkeypatch.setattr(shot_video_jobs, '_video_config', lambda: {
        'provider': 'minimax', 'model': 'MiniMax-H3', 'resolution': '768P',
    })
    monkeypatch.setattr(video_first, 'lookup_shot', lambda *a: {})
    inputs = [{'segment_index': 0, 'text': '鹰嘴豆富含蛋白质。',
               'image_path': str(frame), 'image_status': 'completed'}]
    result = video_first._shot_inputs('episode', inputs, '16:9', 5)
    assert '<d>[Chinese] 鹰嘴豆富含蛋白质。</d>' in result[0]['motion_prompt']


def test_dialogue_budget_retains_action_when_continuity_constraints_are_long(local):
    module = importlib.import_module('src.text.native_dialogue')
    result = module.compile_native_dialogue(
        '角色与场景约束；' * 220 + '动作与运镜：舀起鹰嘴豆并向镜头展示；微笑收尾。',
        '鹰嘴豆富含蛋白质。',
    )
    assert '舀起鹰嘴豆并向镜头展示' in result


def test_legacy_provider_does_not_receive_native_dialogue(local):
    module = importlib.import_module('src.text.native_dialogue')
    original = '安静的环境，镜头缓慢推进'
    assert module.compile_video_prompt('episode', '鹰嘴豆', original, None,
                                       {'provider': 'wan'}) == original


def test_ip_voice_api_hides_private_path_and_keeps_previous_snapshot(local):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api.ip_routes import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    ip = ip_library.save_profile({'name': 'Vivian'})
    url = f'/ai/native/video/kepu/ip-profiles/{ip["ip_id"]}/voice-reference'
    response = client.post(url, files={'file': ('voice.wav', wav(), 'audio/wav')})
    assert response.status_code == 200
    assert response.json()['profile']['voice_reference_ready']
    assert 'reference_voice' not in response.json()['profile']
    pinned = ip_library.bind_project('previous', ip['ip_id'])
    assert client.delete(url).status_code == 200
    assert Path(pinned['reference_voice']).is_file()
    assert not client.get('/ai/native/video/kepu/ip-profiles').json()['items'][0]['voice_reference_ready']
    assert client.post(url, files={'file': ('broken.wav', b'bad', 'audio/wav')}).status_code == 400


def test_single_shot_uses_same_native_script_and_ip_reference(local, monkeypatch):
    from src.api import shot_video_routes
    frame = local / 'frame.png'
    frame.write_bytes(b'frame')
    ip = ip_library.save_profile({'name': 'Vivian'})
    ip_library.save_voice_reference(ip['ip_id'], wav())
    ip_library.bind_project('episode', ip['ip_id'])
    h3_references.select_mode('episode', 'reference')
    segment = {'segment_index': 0, 'image_path': str(frame), 'text': '鹰嘴豆能做沙拉。'}
    monkeypatch.setattr(shot_video_jobs, '_video_config', lambda: {
        'provider': 'minimax', 'model': 'MiniMax-H3', 'resolution': '768P',
    })
    monkeypatch.setattr(shot_video_routes, '_segment', lambda *a: segment)
    monkeypatch.setattr(shot_video_routes, 'lookup_shot', lambda *a: {})
    monkeypatch.setattr(shot_video_routes, '_view', lambda *a: {'status': 'running'})
    calls = []
    monkeypatch.setattr(shot_video_jobs, 'start', lambda *a, **kw: calls.append((a, kw)))
    asyncio.run(shot_video_routes.generate_shot_video(
        'episode', 0, shot_video_routes.CreateShotVideo(prompt='拿起沙拉', duration=5), None,
    ))
    assert '<d>[Chinese] 鹰嘴豆能做沙拉。</d>' in calls[0][0][3]
    assert '<Audio 1>' in calls[0][0][3]
    assert calls[0][1]['reference_assets']['audio'] == ip_library.project_ip('episode')['reference_voice']


def test_public_media_cannot_expose_ip_voice_metadata(local, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    ip = ip_library.save_profile({'name': 'Vivian'})
    ip_library.save_voice_reference(ip['ip_id'], wav())
    ip_library.bind_project('episode', ip['ip_id'])
    monkeypatch.setattr(api_server, 'legacy_media_dir', local / 'data' / 'media')
    monkeypatch.setattr(api_server, 'output_dir', local / 'output')
    client = TestClient(api_server.app)
    assert client.get('/media/_ip_library/profiles.json').status_code == 404
    assert client.get('/media/_ip_library/projects/episode.json').status_code == 404
