"""No external calls, API credentials, or billing in these tests."""
import time
from pathlib import Path

import pytest

from src.config import Config
from src.media import shot_video_jobs
from src.media.minimax_i2v import MinimaxImageToVideo, MinimaxVideoError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.content = b"{}"
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class FakeSession:
    """Answers the three documented Hailuo endpoints in call order."""

    def __init__(self, query_status="Success", base_resp=None, download_url=None,
                 submit_status=200, submit_body=None):
        self.posted = []
        self.fetched = []
        self.query_status = query_status
        self.base_resp = base_resp or {"status_code": 0, "status_msg": "success"}
        self.download_url = download_url or "https://cdn.hailuoai.com/clip.mp4"
        self.submit_status = submit_status
        self.submit_body = submit_body

    def post(self, url, **kwargs):
        self.posted.append((url, kwargs))
        if self.submit_body is not None:
            return FakeResponse(self.submit_body, self.submit_status)
        return FakeResponse({"task_id": "task-123", "base_resp": self.base_resp})

    def get(self, url, **kwargs):
        self.fetched.append((url, kwargs))
        if url.endswith("/v1/query/video_generation"):
            return FakeResponse({
                "task_id": "task-123", "status": self.query_status,
                "file_id": "file-456", "base_resp": self.base_resp,
            })
        return FakeResponse({
            "file": {"file_id": "file-456", "download_url": self.download_url},
            "base_resp": self.base_resp,
        })


def test_hailuo_payload_uses_first_frame_data_url(tmp_path):
    image = tmp_path / "first.png"
    image.write_bytes(b"test-image")
    session = FakeSession()
    # Hailuo 2.3 is the silent v1 family; pin it so the machine-local config
    # cannot silently switch this contract test to the H3 v2 protocol.
    client = MinimaxImageToVideo(
        api_key="test-key", session=session,
        model="MiniMax-Hailuo-2.3", resolution="1080P",
    )

    assert client.submit(image, "镜头缓慢推进", 6) == "task-123"

    url, sent = session.posted[0]
    assert url == "https://api.minimax.cn/v1/video_generation"
    assert sent["headers"]["Authorization"] == "Bearer test-key"
    assert sent["json"]["model"] == "MiniMax-Hailuo-2.3"
    assert sent["json"]["duration"] == 6
    assert sent["json"]["resolution"] == "1080P"
    assert sent["json"]["first_frame_image"].startswith("data:image/png;base64,")
    # Hailuo has no audio switch; audio must never be requested silently.
    assert "audio" not in sent["json"]

    download = client.wait("task-123", interval=0.001)
    assert download == "https://cdn.hailuoai.com/clip.mp4"
    assert session.fetched[0][0].endswith("/v1/query/video_generation")
    assert session.fetched[0][1]["params"] == {"task_id": "task-123"}
    assert session.fetched[1][0].endswith("/v1/files/retrieve")
    assert session.fetched[1][1]["params"] == {"file_id": "file-456"}


def test_hailuo_duration_depends_on_resolution(tmp_path):
    image = tmp_path / "first.png"
    image.write_bytes(b"image")
    assert MinimaxImageToVideo.duration_options("1080P", "MiniMax-Hailuo-2.3") == (6,)
    assert MinimaxImageToVideo.duration_options("768P", "MiniMax-Hailuo-2.3") == (6, 10)

    client = MinimaxImageToVideo(
        api_key="dummy", session=FakeSession(), model="MiniMax-Hailuo-2.3",
    )
    with pytest.raises(MinimaxVideoError):
        client.submit(image, "move", duration=5)

    client = MinimaxImageToVideo(
        api_key="dummy", session=FakeSession(), resolution="768P",
        model="MiniMax-Hailuo-2.3",
    )
    assert client.submit(image, "move", duration=10) == "task-123"


def test_hailuo_requires_credentials_and_official_host(tmp_path):
    image = tmp_path / "first.png"
    image.write_bytes(b"image")
    with pytest.raises(MinimaxVideoError):
        MinimaxImageToVideo(api_key=" ", session=FakeSession(), base_url="https://api.minimax.cn")
    with pytest.raises(MinimaxVideoError):
        MinimaxImageToVideo(api_key="key", session=FakeSession(), base_url="http://api.minimax.cn")
    # A trailing /v1 is tolerated, extra paths are not.
    client = MinimaxImageToVideo(api_key="key", session=FakeSession(), base_url="https://api.minimax.cn/v1")
    assert client.base_url == "https://api.minimax.cn"
    with pytest.raises(MinimaxVideoError):
        MinimaxImageToVideo(api_key="key", session=FakeSession(), base_url="https://api.minimax.cn/evil")


def test_hailuo_reports_provider_errors_and_refuses_unsafe_download(tmp_path):
    image = tmp_path / "first.png"
    image.write_bytes(b"image")
    client = MinimaxImageToVideo(api_key="key", session=FakeSession(), base_url="https://api.minimax.cn")
    with pytest.raises(MinimaxVideoError) as error:
        client.download("http://127.0.0.1:2002/private.mp4", tmp_path / "out.mp4")
    assert "HTTPS" in str(error.value)
    with pytest.raises(MinimaxVideoError):
        client.download("https://10.0.0.5/clip.mp4", tmp_path / "out.mp4")

    failing = MinimaxImageToVideo(
        api_key="key", session=FakeSession(),
        base_url="https://api.minimax.cn",
    )
    failing.session.base_resp = {"status_code": 1008, "status_msg": "insufficient balance"}
    with pytest.raises(MinimaxVideoError) as error:
        failing.submit(image, "move", duration=6)
    assert "余额" in str(error.value)

    with pytest.raises(MinimaxVideoError):
        failing.wait("task-123", interval=0.001)


def test_shot_jobs_follow_configured_video_provider(monkeypatch):
    monkeypatch.setattr(
        Config, "load_model_config",
        classmethod(lambda cls: {
            "video": {"provider": "minimax", "resolution": "1080P", "duration": 6},
        }),
    )
    assert shot_video_jobs.video_provider() == "minimax"
    assert shot_video_jobs.provider_class() is MinimaxImageToVideo
    assert shot_video_jobs.duration_options() == (6,)
    assert shot_video_jobs.default_duration() == 6

    monkeypatch.setattr(
        Config, "load_model_config",
        classmethod(lambda cls: {"video": {"provider": "wan"}}),
    )
    assert shot_video_jobs.video_provider() == "wan"
    assert shot_video_jobs.duration_options() == (5, 10, 15)
    assert shot_video_jobs.default_duration() == 5


def test_default_path_instantiates_the_adapter(monkeypatch, tmp_path):
    """Regression: start() must build an adapter instance, not the raw class.

    Calling submit() on the class made every clip fail with a TypeError while
    the checkpoint still looked like a normal provider failure.
    """
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(shot_video_jobs, "video_provider", lambda: "wan")
    instances = []

    class FakeProvider:
        def __init__(self):
            instances.append(self)
        @staticmethod
        def duration_options(resolution=None, model=None):
            return (5, 10, 15)
        def submit(self, image_path, prompt, duration=None):
            return "task-1"
        def wait(self, task_id, timeout=900, interval=6):
            return "https://cdn.hailuoai.com/clip.mp4"
        def download(self, url, output_path):
            Path(output_path).write_bytes(b"\x00\x00\x00\x18ftypisom")
            return str(output_path)

    monkeypatch.setattr(shot_video_jobs, "provider_class", lambda: FakeProvider)
    image = tmp_path / "first.png"
    image.write_bytes(b"image")

    shot_video_jobs.start("task-x", 0, image, "move")
    for _ in range(200):
        if shot_video_jobs.status("task-x", 0, image)["status"] == "completed":
            break
        time.sleep(0.01)

    assert len(instances) == 1
    assert shot_video_jobs.status("task-x", 0, image)["status"] == "completed"


def test_h3_uses_v2_content_array_and_reports_audio(tmp_path):
    """H3 is the audio-capable family and speaks the multimodal v2 protocol."""
    from PIL import Image
    image = tmp_path / "first.png"
    Image.new("RGB", (640, 480)).save(image)          # 4:3 first frame
    session = FakeSession()
    client = MinimaxImageToVideo(api_key="key", session=session, model="MiniMax-H3")

    assert client.resolution == "768P"
    assert client.duration_options("768P", "MiniMax-H3") == tuple(range(4, 16))
    assert MinimaxImageToVideo.supports_audio("MiniMax-H3") is True
    assert MinimaxImageToVideo.supports_audio("MiniMax-Hailuo-2.3") is False
    assert client.submit(image, "a girl writes in her notebook", 8) == "task-123"

    url, sent = session.posted[0]
    assert url == "https://api.minimax.cn/v2/video_generation"
    assert sent["json"]["duration"] == 8
    assert sent["json"]["resolution"] == "768P"
    # The ratio follows the first frame instead of being hardcoded.
    assert sent["json"]["ratio"] == "4:3"
    kinds = [item["type"] for item in sent["json"]["content"]]
    assert kinds == ["text", "image_url"]
    assert sent["json"]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    # The silent v1 protocol fields must not leak into an H3 request.
    assert "first_frame_image" not in sent["json"]
    assert "prompt" not in sent["json"]


def test_h3_ratio_falls_back_when_the_frame_is_unreadable(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not-an-image")
    assert MinimaxImageToVideo._ratio_for_image(broken) == "16:9"


def test_h3_surfaces_http_error_payload(tmp_path):
    image = tmp_path / "first.png"
    image.write_bytes(b"image")
    session = FakeSession(
        submit_status=400,
        submit_body={"error": {"type": "bad_request_error",
                              "message": "invalid params, model MiniMax-H3 does not "
                                         "support resolution 1080P, supported "
                                         "resolutions: 480P, 768P, 2K (2013)"}},
    )
    client = MinimaxImageToVideo(api_key="key", session=session, model="MiniMax-H3")
    with pytest.raises(MinimaxVideoError) as error:
        client.submit(image, "move", duration=6)
    message = str(error.value)
    assert "参数" in message
    assert "2K" in message
