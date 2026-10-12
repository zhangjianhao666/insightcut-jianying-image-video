"""H3 scene/voice references are local, explicit and paid-submission-safe."""
import io
import time
import wave
from pathlib import Path

import pytest
from PIL import Image

from src.config import Config
from src.media import h3_references, shot_video_jobs, video_first
from src.media.minimax_i2v import MinimaxImageToVideo, MinimaxVideoError


def picture(color="blue"):
    stream = io.BytesIO()
    Image.new("RGB", (640, 360), color).save(stream, "PNG")
    return stream.getvalue()


def voice(seconds=3, rate=16000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(round(rate * seconds) * 2))
    return stream.getvalue()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(h3_references, "project_ip", lambda task_id: None)
    return tmp_path


def test_reference_is_off_by_default_and_persisted(root):
    frame = root / "frame.png"
    frame.write_bytes(picture())
    assert h3_references.mode("one") == "first_frame"
    assert h3_references.for_shot("one", frame, "scene_01") is None
    h3_references.select_mode("one", "reference")
    assert h3_references.mode("one") == "reference"
    shot = h3_references.for_shot("one", frame, "scene_01")
    assert shot["mode"] == "reference"
    assert shot["images"] == [str(frame)]
    assert shot["audio"] is None
    assert shot["fingerprint"] != "first_frame"
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.select_mode("one", "auto")


def test_shared_scene_and_voice_are_reused_by_distinct_shots(root):
    h3_references.select_mode("episode", "reference")
    h3_references.save_scene_image("episode", "cafe_01", picture("orange"))
    h3_references.save_voice_sample("episode", voice(4))
    a, b = root / "a.png", root / "b.png"
    a.write_bytes(picture("red"))
    b.write_bytes(picture("green"))
    shot_a = h3_references.for_shot("episode", a, "cafe_01")
    shot_b = h3_references.for_shot("episode", b, "cafe_01")
    assert shot_a["images"][0] != shot_b["images"][0]
    assert shot_a["images"][1] == shot_b["images"][1]
    assert shot_a["audio"] == shot_b["audio"]
    overview = h3_references.overview("episode", ["cafe_01", "room_02"])
    assert overview["mode"] == "reference"
    assert overview["voice_ready"]
    assert overview["scenes"][0]["image_ready"]
    assert not overview["scenes"][1]["image_ready"]
    assert "images" not in overview and "audio" not in overview
    assert "data" not in overview


def test_reference_changes_make_new_fingerprint_without_mutating_old_media(root):
    frame = root / "frame.png"
    frame.write_bytes(picture())
    h3_references.select_mode("episode", "reference")
    h3_references.save_scene_image("episode", "scene_01", picture("white"))
    original = h3_references.for_shot("episode", frame, "scene_01")
    h3_references.save_scene_image("episode", "scene_01", picture("black"))
    newer = h3_references.for_shot("episode", frame, "scene_01")
    assert newer["fingerprint"] != original["fingerprint"]
    assert Path(original["images"][1]).is_file()
    h3_references.save_voice_sample("episode", voice(3))
    newer_voice = h3_references.for_shot("episode", frame, "scene_01")
    assert newer_voice["fingerprint"] != newer["fingerprint"]
    h3_references.clear_voice("episode")
    assert h3_references.for_shot("episode", frame, "scene_01")["audio"] is None


def test_rejects_invalid_user_media_and_paths(root):
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.save_scene_image("episode", "../secrets", picture())
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.save_scene_image("episode", "scene_01", b"invalid-file")
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.save_scene_image("episode", "scene_01", bytes(12 * 1024 * 1024 + 1))
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.save_voice_sample("episode", voice(1))
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.save_voice_sample("episode", voice(16))
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.save_voice_sample("episode", b"this is not a WAV")
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.overview("../unsafe")
    with pytest.raises(h3_references.H3ReferenceError):
        h3_references.select_mode("../unsafe", "reference")


def test_includes_versioned_project_ip_reference(root, monkeypatch):
    frame = root / "shot.png"
    frame.write_bytes(picture("green"))
    ip = root / "ip.jpg"
    Image.new("RGB", (640, 640)).save(ip, "JPEG")
    monkeypatch.setattr(
        h3_references, "project_ip",
        lambda task_id: {"reference_image": str(ip), "name": "original IP"},
    )
    h3_references.select_mode("episode", "reference")
    shot = h3_references.for_shot("episode", frame, "scene_01")
    assert len(shot["images"]) == 2
    assert shot["images"][1] == str(ip)
    assert h3_references.overview("episode")["ip_ready"]


def test_h3_adapter_sends_reference_roles_not_first_frame(root):
    frame = root / "frame.png"
    frame.write_bytes(picture())
    h3_references.select_mode("episode", "reference")
    h3_references.save_scene_image("episode", "scene_01", picture("orange"))
    h3_references.save_voice_sample("episode", voice(4))
    refs = h3_references.for_shot("episode", frame, "scene_01")
    client = MinimaxImageToVideo(api_key="test-only", model="MiniMax-H3")
    url, payload = client._payload(frame, "A woman turns in the cafe", 6, reference_assets=refs)
    assert url == "/v2/video_generation"
    roles = [entry.get("role") for entry in payload["content"]]
    assert roles == [None, "reference_image", "reference_image", "reference_audio"]
    assert "first_frame" not in roles and "last_frame" not in roles
    assert payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert payload["content"][-1]["audio_url"]["url"].startswith("data:audio/wav;base64,")
    assert payload["ratio"] == "16:9"
    _, regular = client._payload(frame, "hello", 6)
    assert regular["content"][1]["role"] == "first_frame"
    assert "reference_image" not in [item.get("role") for item in regular["content"]]
    legacy = MinimaxImageToVideo(api_key="test-only", model="MiniMax-Hailuo-2.3")
    with pytest.raises(MinimaxVideoError, match="只有 MiniMax-H3"):
        legacy._payload(frame, "hello", 6, reference_assets=refs)


def test_h3_max_resolution_duration_contract(root):
    assert MinimaxImageToVideo.resolution_options("MiniMax-H3") == ("768P", "2K")
    assert MinimaxImageToVideo.resolution_options("MiniMax-H3-Max") == ("480P", "768P")
    assert 4 in MinimaxImageToVideo.duration_options(model="MiniMax-H3")
    assert 4 not in MinimaxImageToVideo.duration_options(model="MiniMax-H3-Max")


def test_shot_jobs_only_reuse_same_reference_fingerprint(root, monkeypatch):
    monkeypatch.setattr(
        Config, "load_model_config",
        classmethod(lambda cls: {"video": {
            "provider": "minimax", "model": "MiniMax-H3", "resolution": "768P"
        }}),
    )
    image = root / "frame.png"
    image.write_bytes(picture())
    h3_references.select_mode("episode", "reference")
    reference = h3_references.for_shot("episode", image, "scene_01")
    calls = []

    class FakeProvider:
        def submit(self, image_path, prompt, duration=None, reference_assets=None):
            calls.append(reference_assets["fingerprint"])
            return f"remote-{len(calls)}"
        def wait(self, task_id, timeout=900, interval=6):
            return "https://cdn.example.com/result.mp4"
        def download(self, url, output_path):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"mock")
            return output_path

    class Factory:
        def __call__(self):
            return FakeProvider()

    def ready():
        for _ in range(200):
            if shot_video_jobs.status("episode", 0, image).get("status") == "completed":
                return
            time.sleep(0.01)
        pytest.fail("mocked shot generation did not finish")

    shot_video_jobs.start(
        "episode", 0, image, "motion", duration=6,
        provider_factory=Factory(), reference_assets=reference,
    )
    ready()
    shot_video_jobs.start(
        "episode", 0, image, "motion", duration=6,
        provider_factory=Factory(), reference_assets=reference,
    )
    assert len(calls) == 1
    h3_references.save_voice_sample("episode", voice(5))
    new_ref = h3_references.for_shot("episode", image, "scene_01")
    shot_video_jobs.start(
        "episode", 0, image, "motion", duration=6,
        provider_factory=Factory(), reference_assets=new_ref,
    )
    ready()
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert shot_video_jobs.status("episode", 0, image)["reference_fingerprint"] == new_ref["fingerprint"]


def test_full_video_marks_changed_scene_assets_stale(root, monkeypatch):
    monkeypatch.setattr(
        Config, "load_model_config",
        classmethod(lambda cls: {"video": {
            "provider": "minimax", "model": "MiniMax-H3", "resolution": "768P"
        }}),
    )
    monkeypatch.setattr(video_first, "lookup_shot", lambda task_id, idx, text: {
        "scene_id": "scene_01", "motion_prompt": "steady camera",
    })
    monkeypatch.setattr(video_first, "load_plan", lambda task_id: {})
    frame = root / "frame.png"
    frame.write_bytes(picture())
    segments = [{
        "segment_index": 0, "text": "scene test",
        "image_path": str(frame), "image_status": "completed",
    }]
    h3_references.select_mode("episode", "reference")
    shot = video_first._shot_inputs("episode", segments, "16:9", 6)[0]
    output = root / "mock.mp4"
    output.write_bytes(b"mock")
    video_first._write("episode", {
        "status": "completed", "shots": [shot],
        "ratio": "16:9", "duration": 6, "output_path": str(output),
    })
    # No per-shot file exists, but we only test invalidation of the reference.
    h3_references.save_scene_image("episode", "scene_01", picture("orange"))
    assert video_first.status("episode", segments, ratio="16:9")["status"] == "stale"
