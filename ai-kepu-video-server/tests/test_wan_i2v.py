"""No external calls, API credentials, or billing in these tests."""
import time
from pathlib import Path

import pytest

from src.media.shot_video_jobs import start, status, matching_video_path, _write
from src.media.wan_i2v import WanImageToVideo, WanVideoError
from src.config import Config


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.submitted = []
    def post(self, url, **kwargs):
        self.submitted.append((url, kwargs))
        return FakeResponse({"output": {"task_id": "task-123"}})
    def get(self, url, **kwargs):
        return FakeResponse({"output": {"task_status": "SUCCEEDED", "video_url": "https://video.oss-cn-beijing.aliyuncs.com/clip.mp4"}})


def test_wan_payload_disables_model_audio(tmp_path):
    image = tmp_path / "first.png"
    image.write_bytes(b"test-image")
    session = FakeSession()
    client = WanImageToVideo(api_key="test-key", session=session)
    assert client.submit(image, "镜头缓慢推进", 6) == "task-123"
    url, sent = session.submitted[0]
    assert url.endswith("/services/aigc/video-generation/video-synthesis")
    assert sent["json"]["parameters"]["audio"] is False
    assert sent["json"]["parameters"]["duration"] == 6
    assert sent["json"]["input"]["img_url"].startswith("data:image/png;base64,")
    assert client.wait("task-123", interval=0.001) == "https://video.oss-cn-beijing.aliyuncs.com/clip.mp4"


def test_wan_rejects_unsupported_duration(tmp_path):
    image = tmp_path / "first.png"
    image.write_bytes(b"image")
    client = WanImageToVideo(api_key="dummy", session=FakeSession())
    with pytest.raises(WanVideoError):
        client.submit(image, "move", duration=16)


def test_shot_checkpoint_and_stale_image(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    image = tmp_path / "first.png"
    image.write_bytes(b"image-one")
    calls = []
    class FakeProvider:
        def submit(self, image_path, prompt, duration):
            calls.append("submit")
            return "task-1"
        def wait(self, task_id):
            calls.append("wait")
            return "https://example.invalid/clip.mp4"
        def download(self, url, path):
            Path(path).write_bytes(b"\x00\x00\x00\x18ftypisom")
            calls.append("download")
    first = start("example", 0, image, "镜头移动", provider_factory=FakeProvider)
    assert first["status"] == "running"
    for _ in range(100):
        row = status("example", 0, image)
        if row.get("status") == "completed":
            break
        time.sleep(0.01)
    assert row["status"] == "completed", row
    assert calls == ["submit", "wait", "download"]
    assert matching_video_path("example", {"segment_index": 0, "image_path": str(image)}) == row["video_path"]
    before = start("example", 0, image, "镜头移动", provider_factory=FakeProvider)
    assert before["status"] == "completed"
    assert calls.count("submit") == 1
    image.write_bytes(b"image-two-larger")
    assert status("example", 0, image)["status"] == "stale"
    assert matching_video_path("example", {"segment_index": 0, "image_path": str(image)}) is None


def test_interrupted_job_reuses_original_task(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    image = tmp_path / "first.png"
    image.write_bytes(b"image")
    from src.media.shot_video_jobs import _identity
    _write("example", 1, {
        "status": "running", "provider_task_id": "saved-task",
        "image_signature": _identity(image),
        "prompt": "move", "duration": 5,
        "video_path": str(tmp_path / "older.mp4"),
    })
    assert status("example", 1, image)["status"] == "interrupted"
    calls = []
    class FakeProvider:
        def submit(self, *args, **kwargs):
            raise AssertionError("must not submit a second paid job")
        def wait(self, task_id):
            calls.append(task_id)
            return "https://example.invalid/file.mp4"
        def download(self, url, path):
            Path(path).write_bytes(b"\x00\x00\x00\x18ftypisom")
    start("example", 1, image, "move", provider_factory=FakeProvider)
    for _ in range(100):
        if status("example", 1, image)["status"] == "completed":
            break
        time.sleep(0.01)
    assert calls == ["saved-task"]


def test_ffmpeg_uses_dynamic_video_and_drops_model_audio(monkeypatch, tmp_path):
    """Dynamic clips must follow TTS duration instead of using model audio."""
    import subprocess
    import wave
    from src.export.ffmpeg_exporter import FFmpegExporter
    model_clip = tmp_path / "model.mp4"
    model_clip.write_bytes(b"sample")
    audio = tmp_path / "narration.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\\x00\\x00" * 24000)
    exporter = FFmpegExporter(canvas={"width": 640, "height": 360, "fps": 24})
    commands = []
    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"rendered")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(exporter, "_run_command", fake_run)
    result = exporter._build_segment_clip(
        index=0, image_path=str(model_clip), audio_path=str(audio),
        text="测试镜头", anim_type=0, scale_end=1.0, tmp_dir=str(tmp_path),
    )
    assert Path(result).is_file()
    command = commands[0]
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert command.count("-map") == 2
    assert "1:a" in command
    assert command[command.index("-t") + 1] == "1.000"
