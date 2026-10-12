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
            "motion_prompt": "主持人抬头；小狗摇尾；[Static shot]；结尾对视",
        },
        {
            "index": 1,
            "scene_id": "scene_01", "character_ids": ["dog"],
            "location_id": "living_room", "camera": "closeup shot",
            "start_state": "小狗在原来位置抬头", "end_state": "小狗看向左边",
            "transition_to_next": "match_cut", "shot_type": "portrait",
            "image_prompt": "Same little white dog in the warm room looking towards the host, close up portrait",
            "motion_prompt": "小狗抬头；目光转移；[Push in]；收尾保持正面",
        },
        {
            "index": 2,
            "scene_id": "scene_02", "character_ids": ["host"],
            "location_id": "school", "camera": "wide shot",
            "start_state": "主持人站在黑板旁", "end_state": "主持人拿起粉笔",
            "transition_to_next": "continue", "shot_type": "scene",
            "image_prompt": "Same brown-haired host with fixed clothes standing in classroom holding a chalk",
            "motion_prompt": "拿粉笔；动作缓慢；[Pan left]；镜头结束在黑板",
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
    assert first["version"] == 3
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
