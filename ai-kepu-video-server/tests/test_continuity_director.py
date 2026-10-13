"""Continuity director regression checks. Never calls a paid model endpoint."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.config import Config
from src.media import video_first
from src.text import continuity, storyboard_director


def planned_shots():
    return [
        {
            "index": 0,
            "scene_id": "scene_01", "character_ids": ["host", "dog"],
            "location_id": "living_room", "camera": "wide shot",
            "start_state": "主持人坐在桌边", "end_state": "主持人抬头看向小狗",
            "transition_to_next": "cut", "shot_type": "scene",
            "image_prompt": "Brown haired host and white dog in a cozy room with warm colors, wide shot",
            "motion_prompt": "主持人坐在温暖客厅的桌边，保持固定的棕色头发、酒红色上衣和二维卡通画风，桌面道具与柔和侧光保持一致；主持人先缓缓抬头看向小狗，右手从桌面抬起指向面前道具，小狗轻轻摇尾并向主持人投去好奇目光，动作按顺序展开且幅度自然；[Static shot] 镜头保持全景与原有轴线，背景窗帘随微风轻动，光影平稳不过曝；结尾两者对视，主持人的手停在道具旁，小狗保持原站位，留下清晰稳定的收尾构图。",
        },
        {
            "index": 1,
            "scene_id": "scene_01", "character_ids": ["dog"],
            "location_id": "living_room", "camera": "closeup shot",
            "start_state": "小狗在原来位置抬头", "end_state": "小狗看向左边",
            "transition_to_next": "match_cut", "shot_type": "portrait",
            "image_prompt": "Same little white dog in the warm room looking towards the host, close up portrait",
            "motion_prompt": "小狗仍在客厅原来位置，保持白色毛发、固定体型以及温暖室内光线，画风继续使用粗黑轮廓和扁平色彩；小狗先轻轻抬头，耳朵随动作微微摆动，再将目光从主持人转向画面左边，前爪保持落地，身体不突然移动，尾巴有节奏地轻摆；[Push in] 镜头沿原轴线缓慢推进到面部特写，保留背景桌角的位置关系，景深逐渐柔化但主体保持清晰；收尾保持正面姿态，目光停留在左侧，耳朵与尾巴缓缓静止，给后续匹配剪辑留下稳定的形状与视线方向。",
        },
        {
            "index": 2,
            "scene_id": "scene_02", "character_ids": ["host"],
            "location_id": "school", "camera": "wide shot",
            "start_state": "主持人站在黑板旁", "end_state": "主持人拿起粉笔",
            "transition_to_next": "continue", "shot_type": "scene",
            "image_prompt": "Same brown-haired host with fixed clothes standing in classroom holding a chalk",
            "motion_prompt": "主持人站在教室黑板旁，保持同样的棕色头发、酒红色上衣和温和表情，黑板与讲台的位置固定，环境保持二维卡通画风；主持人先向讲台伸出右手，缓慢拿起一支粉笔，将粉笔举到胸前并转头看向黑板，左手自然垂在身侧，动作连续平稳，避免道具凭空变化；[Pan left] 镜头从主持人所在位置缓慢向左摇移，逐渐将黑板纳入主体构图，室内柔光保持方向不变；镜头结束在黑板与主持人侧身的同框画面，右手握住粉笔停在黑板前，身体与视线稳定，留出清晰的下一镜头开场状态。",
        },
    ]


def fake_result(shots):
    return {
        "visual_bible": {
            "character_lock": "Same brown-haired original host in a burgundy top",
            "environment_lock": "Recurring room",
            "art_direction": "Flat American 2D cartoon with thick black outline",
            "camera_language": "Wide and close-ups",
            "negative_rules": "No captions",
        },
        "shots": shots,
    }


def test_cut_does_not_force_identical_consecutive_frames():
    plan = storyboard_director._normalize_plan(fake_result(planned_shots()), 3)
    first = plan["shots"]["0"]
    assert first["transition_to_next"] == "cut"
    assert plan["shots"]["1"]["scene_id"] == "scene_01"
    motion = continuity.compose_motion_prompt(plan, 1, "用户指定特写镜头，抬头；[Push in]")
    assert "正常切镜" in motion
    assert "不要复制上一段的尾帧" in motion
    assert "用户指定特写镜头" in motion
    assert "主持人抬头看向小狗" not in motion  # no physical frame-equality demand
    assert "Flat American 2D cartoon" in motion
    assert len(motion) <= 1800


def test_match_cut_matches_motif_not_pixels():
    plan = storyboard_director._normalize_plan(fake_result(planned_shots()), 3)
    result = continuity.compose_motion_prompt(plan, 2, "镜头动作")
    assert "匹配剪辑" in result
    assert "不要求两帧像素相同" in result
    assert "小狗看向左边" in result
    assert "镜头动作" in result


def test_continuous_action_requires_same_scene():
    shots = planned_shots()
    shots[0]["transition_to_next"] = "continue"
    shots[1]["scene_id"] = "scene_02"
    plan = storyboard_director._normalize_plan(fake_result(shots), 3)
    assert plan["shots"]["0"]["transition_to_next"] == "cut"
    assert plan["shots"]["2"]["transition_to_next"] == "cut"


def test_same_scene_continue_adds_action_anchors():
    shots = planned_shots()
    shots[0]["transition_to_next"] = "continue"
    plan = storyboard_director._normalize_plan(fake_result(shots), 3)
    prompt = continuity.compose_motion_prompt(plan, 1, "向主人抬头")
    assert "连续动作镜头" in prompt
    assert "主持人抬头看向小狗" in prompt
    assert "小狗在原来位置抬头" in prompt


def test_missing_states_are_visible_and_bad_fields_are_bounded():
    bad = planned_shots()
    bad[0]["start_state"] = ""
    bad[0]["character_ids"] = "not-a-list"
    bad[0]["scene_id"] = "../../secrets"
    bad[0]["transition_to_next"] = "dissolve"
    plan = storyboard_director._normalize_plan(fake_result(bad), 3)
    assert plan["shots"]["0"]["scene_id"] == "scene_000"
    assert plan["shots"]["0"]["character_ids"] == []
    assert plan["shots"]["0"]["transition_to_next"] == "cut"
    assert any(w["code"] == "state_missing" for w in continuity.audit_plan(plan))


def test_global_plan_reuse_and_ip_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    class FakeModel:
        def __init__(self):
            self.calls = []
        def _call_api(self, **kwargs):
            self.calls.append(kwargs)
            return json.dumps(fake_result(planned_shots()), ensure_ascii=False)
    fake = FakeModel()
    kwargs = {
        "script": "讲钱的故事", "segments": ["开头", "钱钱的建议", "总结"],
        "summary": "三镜头讲故事", "style": "二维美式动画", "ratio": "16:9",
    }
    first = storyboard_director.plan_storyboard("series_01", fake, **kwargs)
    next_ = storyboard_director.plan_storyboard("series_01", fake, **kwargs)
    assert len(fake.calls) == 1
    assert first["version"] == storyboard_director._PLAN_VERSION
    assert next_["shots"] == first["shots"]
    assert first["shots"]["0"]["start_state"] == "主持人坐在桌边"
    assert "不要假定每个镜头的尾帧必须等于" in fake.calls[0]["messages"][0]["content"]
    assert storyboard_director.lookup_shot("series_01", 0, "新文案") == {}


def test_video_first_submits_compiled_prompt_to_video_job(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(video_first.shot_video_jobs, "duration_options", lambda: (5, 10, 15))
    class Model:
        def _call_api(self, **kwargs):
            return json.dumps(fake_result(planned_shots()), ensure_ascii=False)
    story_text = ["开头", "钱钱的建议", "总结"]
    storyboard_director.plan_storyboard(
        "project", Model(), script="一集故事", segments=story_text,
        summary="小狗钱钱", style="2D cartoon", ratio="16:9",
    )
    segments = []
    for i in range(3):
        file = tmp_path / f"frame_{i}.png"
        file.write_bytes(b"test")
        segments.append({
            "segment_index": i, "text": story_text[i],
            "image_path": str(file), "image_status": "completed",
        })
    shots = video_first._shot_inputs("project", segments, "16:9", 5)
    assert "正常切镜" in shots[1]["motion_prompt"]
    assert "匹配剪辑" in shots[2]["motion_prompt"]
    assert shots[0]["scene_id"] == "scene_01"
    assert shots[0]["transition_to_next"] == "cut"
    assert "Same brown-haired original host" in shots[0]["motion_prompt"]


def test_compiled_motion_change_invalidates_completed_video(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(video_first.shot_video_jobs, "duration_options", lambda: (5, 10, 15))
    monkeypatch.setattr(video_first.shot_video_jobs, "status",
                        lambda task_id, index, img: {"status": "completed"})
    image = tmp_path / "frame.png"
    image.write_bytes(b"abc")
    segment = {
        "segment_index": 0, "text": "故事开头", "image_path": str(image),
        "image_status": "completed",
    }
    shots = video_first._shot_inputs("project", [segment], "16:9", 5)
    output = tmp_path / "finished.mp4"
    output.write_bytes(b"mp4")
    record = {
        "status": "completed", "fingerprint": "test",
        "shots": shots, "ratio": "16:9", "duration": 5,
        "output_path": str(output),
    }
    video_first._write("project", record)
    assert video_first.status("project", [segment], ratio="16:9")["status"] == "completed"
    monkeypatch.setattr(video_first, "lookup_shot",
                        lambda task_id, index, text: {"motion_prompt": "新的运镜"})
    assert video_first.status("project", [segment], ratio="16:9")["status"] == "stale"


def test_audio_policy_persisted_for_automatic_producer(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    assert video_first.audio_preference("project") == "native"
    assert video_first.set_audio_preference("project", "mute") == "mute"
    assert video_first.audio_preference("project") == "mute"
    assert video_first.status("project", [])["preferred_audio_mode"] == "mute"
    with pytest.raises(video_first.VideoFirstError):
        video_first.set_audio_preference("project", "custom-provider")
    assert video_first.audio_preference("project") == "mute"


def test_completed_clip_must_match_exact_director_prompt_and_duration():
    shot = {"motion_prompt": "新导演脚本", "duration": 5}
    assert video_first._matches_planned_clip(
        {"status": "completed", "prompt": "新导演脚本", "duration": 5}, shot
    )
    assert not video_first._matches_planned_clip(
        {"status": "completed", "prompt": "旧导演脚本", "duration": 5}, shot
    )
    assert not video_first._matches_planned_clip(
        {"status": "completed", "prompt": "新导演脚本", "duration": 10}, shot
    )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="needs system FFmpeg tools",
)
def test_mute_mode_drops_h3_native_audio_without_changing_source(tmp_path):
    clip = tmp_path / "with_audio.mp4"
    subprocess.run([
        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1.1",
        "-f", "lavfi", "-i", "sine=frequency=480:sample_rate=24000:duration=1.1",
        "-map", "0:v", "-map", "1:a", "-c:v", "mpeg4", "-c:a", "aac",
        "-shortest", str(clip),
    ], check=True, timeout=30)
    output = tmp_path / "mute.mp4"
    video_first.render_clips([clip], output, audio_mode="mute")
    result = subprocess.run([
        shutil.which("ffprobe"), "-v", "error", "-show_entries",
        "stream=codec_type", "-of", "csv=p=0", str(output),
    ], capture_output=True, text=True, check=True, timeout=30)
    assert result.stdout.strip() == "video"
    original = subprocess.run([
        shutil.which("ffprobe"), "-v", "error", "-show_entries",
        "stream=codec_type", "-of", "csv=p=0", str(clip),
    ], capture_output=True, text=True, check=True, timeout=30)
    assert "audio" in original.stdout

@pytest.mark.parametrize('response,stage,category', [
    ('not json', 'parse', 'invalid_json'),
    (json.dumps(fake_result([])), 'validation', 'incomplete_shots'),
    (json.dumps(fake_result(planned_shots()[:1])), 'validation', 'incomplete_shots'),
])
def test_failed_short_plan_is_diagnosed_and_retried(monkeypatch, tmp_path, response, stage, category):
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    class Model:
        calls = 0
        def _call_api(self, **kwargs):
            self.calls += 1
            return response
    model = Model()
    kwargs = dict(script='短文', segments=['第一句', '第二句'], summary='总结', style='动画', ratio='16:9')
    result = storyboard_director.plan_storyboard('diagnostic', model, **kwargs)
    assert result['status'] == 'failed'
    assert result['error'] == {'stage': stage, 'category': category}
    assert storyboard_director.load_plan('diagnostic')['error'] == result['error']
    assert storyboard_director.lookup_shot('diagnostic', 0) == {}
    storyboard_director.plan_storyboard('diagnostic', model, **kwargs)
    assert model.calls == 2


def test_provider_failure_diagnostics_never_copy_exception(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    class Model:
        def _call_api(self, **kwargs):
            raise TimeoutError('api_key=secret-provider-response')
    result = storyboard_director.plan_storyboard('safe_diagnostic', Model(), script='文', segments=['一句'], summary='', style='', ratio='16:9')
    assert {key: result['error'][key] for key in ('stage', 'category')} == {'stage': 'provider', 'category': 'timeout'}
    assert result['error']['retryable'] is True
    assert '超时' in result['error']['safe_message']
    assert 'safe_diagnostic' in caplog.text
    assert 'secret-provider-response' not in caplog.text
    assert 'secret-provider-response' not in json.dumps(result)


def test_missing_motion_script_is_not_successful_director_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    shots = planned_shots()[:1]
    shots[0]['motion_prompt'] = ''
    class Model:
        def _call_api(self, **kwargs):
            return json.dumps(fake_result(shots))
    result = storyboard_director.plan_storyboard('missing_motion', Model(), script='文', segments=['一句'], summary='', style='', ratio='16:9')
    assert result['status'] == 'failed'
    assert result['error'] == {'stage': 'validation', 'category': 'invalid_shot_structure'}


def test_short_plan_budget_includes_reasoning_and_detailed_shots(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    class Model:
        budget = None
        def _call_api(self, **kwargs):
            self.budget = kwargs['max_tokens']
            return json.dumps(fake_result(planned_shots()[:2]))
    model = Model()
    storyboard_director.plan_storyboard('reasoning', model, script='文', segments=['一句', '二句'], summary='', style='', ratio='16:9')
    assert model.budget >= 8000


def test_review_exposes_failed_director_without_provider_details(monkeypatch):
    from src.api import continuity_routes as routes
    monkeypatch.setattr(routes.mysql_client, 'get_task', lambda _: {'task_id': 'failed'})
    monkeypatch.setattr(routes.mysql_client, 'get_segments', lambda _: [{'segment_index': 0, 'text': '一句'}])
    monkeypatch.setattr(routes, 'load_plan', lambda _: {'status': 'failed', 'error': {'stage': 'parse', 'category': 'invalid_json'}, 'shots': {}})
    report = routes._report('failed')
    assert report['status'] == 'failed'
    assert report['planning_error'] == {'stage': 'parse', 'category': 'invalid_json'}
    assert report['can_replan']


def test_explicit_replan_is_background_and_preserves_assets(monkeypatch, tmp_path):
    import asyncio
    from src.api import continuity_routes as routes
    from src.api.task_runtime import TaskRuntimeRegistry
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    task = {'task_id': 'replan', 'script_text': '原文', 'summary': '总结', 'style': '卡通', 'ratio': '16:9'}
    segments = [{'segment_index': 0, 'text': '第一句', 'image_path': '/saved/frame.png'}]
    updates = []
    monkeypatch.setattr(routes.mysql_client, 'get_task', lambda _: task)
    monkeypatch.setattr(routes.mysql_client, 'get_segments', lambda _: segments)
    monkeypatch.setattr(routes.mysql_client, 'production_list', lambda **_: [])
    monkeypatch.setattr(routes.mysql_client, 'get_active_task_operation', lambda _: {})
    monkeypatch.setattr(routes.mysql_client, 'create_task_operation', lambda **_: {'outcome': 'created', 'operation': {'operation_id': 'op', 'state': 'pending'}})
    monkeypatch.setattr(routes.mysql_client, 'update_task_operation', lambda op, **values: updates.append(values) or True)
    monkeypatch.setattr(video_first, 'status', lambda *args, **_: {'status': 'not_generated'})
    monkeypatch.setattr(video_first.shot_video_jobs, 'status', lambda *args: {'status': 'not_generated'})
    monkeypatch.setattr(routes, 'task_runtime', TaskRuntimeRegistry(), raising=False)
    queued = []
    class Pool:
        def submit(self, function, *args):
            queued.append((function, args))
            class Future:
                def cancel(self): return False
            return Future()
    monkeypatch.setattr(routes, 'generation_pool', Pool(), raising=False)
    class Model:
        def _call_api(self, **_): return json.dumps(fake_result(planned_shots()[:1]))
    monkeypatch.setattr(routes, 'ArticleGenerator', Model, raising=False)
    result = asyncio.run(routes.replan_director('replan'))
    assert result['status'] == 'planning'
    assert result['operation']['operation_id'] == 'op'
    assert not storyboard_director.load_plan('replan')
    assert len(queued) == 1
    monkeypatch.setattr(routes.mysql_client, 'get_active_task_operation', lambda _: {'operation_id': 'op', 'kind': 'director_replan', 'state': 'pending'})
    assert routes._report('replan')['status'] == 'planning'
    queued[0][0](*queued[0][1])
    assert storyboard_director.load_plan('replan')['status'] == 'completed'
    assert updates[-1]['state'] == 'completed'
    assert segments[0]['image_path'] == '/saved/frame.png'
    assert not routes.task_runtime.is_running('replan')


@pytest.mark.parametrize('active', ['video', 'production'])
def test_replan_rejects_active_video_production(monkeypatch, active):
    import asyncio
    from fastapi import HTTPException
    from src.api import continuity_routes as routes
    monkeypatch.setattr(routes.mysql_client, 'get_task', lambda _: {'ratio': '16:9'})
    monkeypatch.setattr(routes.mysql_client, 'get_segments', lambda _: [{'segment_index': 0, 'text': '文'}])
    monkeypatch.setattr(routes.mysql_client, 'production_list', lambda **_: [{}] if active == 'production' else [])
    monkeypatch.setattr(video_first, 'status', lambda *args, **_: {'status': 'generating' if active == 'video' else 'idle'})
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes.replan_director('busy'))
    assert caught.value.status_code == 409


def test_replan_constructor_failure_is_visible_and_safe(monkeypatch, tmp_path):
    from src.api import continuity_routes as routes
    from src.api.task_runtime import TaskRuntimeRegistry
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(routes, 'task_runtime', runtime)
    updates = []
    monkeypatch.setattr(routes.mysql_client, 'update_task_operation', lambda _, **values: updates.append(values))
    def bad_model(): raise ValueError('api_key=provider-secret')
    monkeypatch.setattr(routes, 'ArticleGenerator', bad_model)
    token = runtime.begin('broken')
    routes._replan_worker('broken', 'op', token, {}, ['文'])
    plan = storyboard_director.load_plan('broken')
    assert plan['status'] == 'failed'
    assert plan['error']['stage'] == 'planning'
    assert 'provider-secret' not in json.dumps(plan)
    assert 'provider-secret' not in json.dumps(updates)
    assert not runtime.is_running('broken')


def test_short_motion_with_full_coverage_is_not_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    shots = planned_shots()[:1]
    shots[0]['motion_prompt'] = '保持角色；自然抬手；[Static shot]；保持稳定'
    class Model:
        def _call_api(self, **_): return json.dumps(fake_result(shots))
    result = storyboard_director.plan_storyboard('short_motion', Model(), script='文', segments=['一句'], summary='', style='', ratio='16:9')
    assert result['status'] == 'failed'
    assert result['error'] == {'stage': 'validation', 'category': 'invalid_shot_structure'}


def test_director_camera_instruction_is_preserved_without_cutting():
    camera = 'Medium close-up, keep the bowl and hands within the frame, follow the existing 180-degree axis, maintain the left window light; never include the face.'
    result = continuity.normalize_shot({'camera': camera}, 0)
    assert result['camera'] == camera


def test_oversized_camera_instruction_is_rejected_not_clipped():
    with pytest.raises(ValueError, match='camera'):
        continuity.normalize_shot({'camera': 'x' * 2001}, 0)
