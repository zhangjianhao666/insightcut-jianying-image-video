"""Real-video-first contract tests. External providers are always mocked."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from src.config import Config
from src.media import shot_video_jobs, video_first


@pytest.fixture
def project(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    # These tests mock the legacy Wan clip lengths; never inherit whatever
    # video provider the developer happens to have configured locally.
    monkeypatch.setattr(shot_video_jobs, "video_provider", lambda: "wan")
    monkeypatch.setattr(
        video_first, "lookup_shot",
        lambda task_id, index, text: {"motion_prompt": "镜头缓慢移动，保持主角外观"}
    )
    inputs = []
    for index in range(2):
        image = tmp_path / f"scene-{index}.png"
        image.write_bytes(b"source-image-" + str(index).encode())
        inputs.append({
            "segment_index": index,
            "text": f"第 {index+1} 段故事",
            "image_status": "completed",
            "image_path": str(image),
        })
    return tmp_path, inputs


def test_requires_explicit_paid_approval(project):
    _, inputs = project
    with pytest.raises(video_first.VideoFirstError, match="确认"):
        video_first.start("test-one", inputs)
    assert video_first.status("test-one", inputs)["status"] == "not_started"


def test_fails_closed_if_first_frame_missing(project):
    _, inputs = project
    inputs[0]["image_status"] = "pending"
    with pytest.raises(video_first.VideoFirstError, match="首帧"):
        video_first.start("test-one", inputs, confirmed=True)


def test_fails_closed_if_dynamic_clip_missing(tmp_path):
    with pytest.raises(video_first.VideoFirstError, match="动态视频片段缺失"):
        video_first.render_clips([tmp_path / "missing.mp4"], tmp_path / "output.mp4")


def test_mocked_two_shot_video_production_and_reuse(monkeypatch, project):
    tmp, inputs = project
    calls = []
    clips = []
    for i in range(2):
        clip = tmp / f"motion-{i}.mp4"
        clip.write_bytes(f"mp4-mock-{i}".encode())
        clips.append(clip)
    done = set()

    def fake_status(task_id, index, image_path):
        return {"status": "completed" if index in done else "not_generated"}

    def fake_start(task_id, index, image_path, prompt, duration, force_new, cancellation=None):
        calls.append((index, duration, prompt, force_new))
        done.add(index)
        return {"status": "running"}

    def fake_matching(task_id, segment):
        index = int(segment["segment_index"])
        return str(clips[index]) if index in done else None

    def fake_render(paths, path, ratio):
        assert [Path(p) for p in paths] == clips
        assert ratio == "16:9"
        output = Path(path)
        output.write_bytes(b"mocked-final-video")
        return str(output)

    monkeypatch.setattr(video_first.shot_video_jobs, "status", fake_status)
    monkeypatch.setattr(video_first.shot_video_jobs, "start", fake_start)
    monkeypatch.setattr(video_first.shot_video_jobs, "matching_video_path", fake_matching)
    monkeypatch.setattr(video_first, "render_clips", fake_render)

    response = video_first.start("project-one", inputs, confirmed=True, duration=5)
    assert response["status"] in {"generating", "rendering", "completed"}
    for _ in range(100):
        status = video_first.status("project-one", inputs, ratio="16:9")
        if status["status"] == "completed":
            break
        time.sleep(.01)
    assert status["status"] == "completed", status
    assert status["completed"] == 2
    assert [x[0] for x in calls] == [0, 1]
    assert status["has_audio"] is False
    assert Path(video_first.completed_path("project-one", inputs, ratio="16:9")).is_file()
    same = video_first.start("project-one", inputs, confirmed=True)
    assert same["status"] == "completed"
    assert len(calls) == 2  # no second paid request

    # Any new image, text, ratio or model clip invalidates final output.
    assert video_first.status("project-one", inputs, ratio="9:16")["status"] == "stale"
    inputs[0]["text"] = "修改的旁白描述"
    assert video_first.status("project-one", inputs, ratio="16:9")["status"] == "stale"
    inputs[0]["text"] = "第 1 段故事"
    clips[0].write_bytes(b"new-version-of-the-motion-clip")
    assert video_first.status("project-one", inputs, ratio="16:9")["status"] == "stale"


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="needs installed FFmpeg tools",
)
def _probe_streams(path):
    probe = subprocess.run([
        shutil.which("ffprobe"), "-v", "error",
        "-show_entries", "stream=codec_type,width,height:format=duration",
        "-of", "json", str(path),
    ], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(probe.stdout)


def _make_clip(path, color, audio):
    command = [
        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d=1.4",
    ]
    if audio:
        command += [
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000:duration=1.4",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path),
        ]
    else:
        command += ["-map", "0:v", "-c:v", "mpeg4", str(path)]
    subprocess.run(command, check=True, timeout=30)


def test_ffmpeg_joins_real_mp4_and_keeps_provider_audio(tmp_path):
    """MiniMax-H3 clips carry their own track; the final render keeps it."""
    clips = []
    for i in range(2):
        clip = tmp_path / f"real-{i}.mp4"
        _make_clip(clip, ["blue", "red"][i], audio=True)
        clips.append(str(clip))

    output = tmp_path / "final.mp4"
    result = video_first.render_clips(clips, output, ratio="16:9")
    assert Path(result).is_file()
    data = _probe_streams(output)
    kinds = [stream["codec_type"] for stream in data["streams"]]
    assert kinds == ["video", "audio"]
    video = data["streams"][0]
    assert video["width"] == 1280 and video["height"] == 720
    assert 2.4 <= float(data["format"]["duration"]) <= 3.5


def test_ffmpeg_renders_silent_provider_clips_without_audio(tmp_path):
    """A silent provider (Wan / Hailuo 2.3) must not invent an audio track."""
    clips = []
    for i in range(2):
        clip = tmp_path / f"silent-{i}.mp4"
        _make_clip(clip, ["green", "yellow"][i], audio=False)
        clips.append(str(clip))

    output = tmp_path / "silent-final.mp4"
    video_first.render_clips(clips, output, ratio="16:9")
    data = _probe_streams(output)
    assert [stream["codec_type"] for stream in data["streams"]] == ["video"]


def test_ffmpeg_pads_a_missing_track_when_only_some_clips_have_audio(tmp_path):
    """A mixed batch must still concat: silent shots get a silent track."""
    first = tmp_path / "with-audio.mp4"
    second = tmp_path / "without-audio.mp4"
    _make_clip(first, "blue", audio=True)
    _make_clip(second, "red", audio=False)

    output = tmp_path / "mixed-final.mp4"
    video_first.render_clips([str(first), str(second)], output, ratio="16:9")
    data = _probe_streams(output)
    assert [stream["codec_type"] for stream in data["streams"]] == ["video", "audio"]
