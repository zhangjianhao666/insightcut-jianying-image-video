from pathlib import Path
"""All tests are offline and do not call a billable image/LLM API."""
import io
import json

from PIL import Image
import pytest

from src.config import Config
from src.media.image_generator import ImageGenerator, GeneratedImagePath
from src.media import wan_image
from src.text.storyboard_director import plan_storyboard, lookup_shot, load_plan


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), (5, 80, 140)).save(output, format="PNG")
    return output.getvalue()


class FakeResponse:
    status_code = 200
    def __init__(self, *, data=None, image=None):
        self.data = data or {}
        self.image = image
    def raise_for_status(self):
        pass
    def json(self):
        return self.data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def iter_content(self, chunk_size):
        yield self.image or b""


class FakeSession:
    def __init__(self):
        self.posts = []
        self.gets = []
    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(data={"output": {
            "choices": [{"message": {"content": [
                {"type": "image", "image": "https://result.oss-cn-beijing.aliyuncs.com/test.png"}
            ]}}]
        }})
    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse(image=png_bytes())


def test_wan_model_payload_and_archiving(tmp_path):
    session = FakeSession()
    adapter = wan_image.WanImageGenerator(
        api_key="test-only", model="wan2.7-image",
        base_url="https://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        session=session,
    )
    result = adapter.generate("same panda in a blue coat", "1280*720", tmp_path / "shot.png")
    assert Image.open(result).size == (32, 24)
    import json
    receipt = json.loads(Path(result).with_suffix(".receipt.json").read_text())
    assert receipt["requested_count"] == receipt["returned_count"] == 1
    url, request = session.posts[0]
    assert url.endswith("/api/v1/services/aigc/multimodal-generation/generation")
    assert request["json"]["parameters"] == {"size": "1280*720", "n": 1, "enable_sequential": False, "watermark": False}
    assert request["json"]["input"]["messages"][0]["content"] == [{"text": "same panda in a blue coat"}]
    assert request["headers"]["Authorization"] == "Bearer test-only"
    assert session.gets[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(("width", "height", "expected"), [
    (1920, 1080, "1280*720"),
    (1080, 1920, "720*1280"),
    (960, 1280, "960*1280"),
    (1000, 1000, "1024*1024"),
])
def test_image_sizes(width, height, expected):
    assert wan_image.image_size(width, height) == expected


def test_wan_rejects_untrusted_image_location(tmp_path):
    session = FakeSession()
    adapter = wan_image.WanImageGenerator(
        api_key="key", base_url="https://demo.cn-beijing.maas.aliyuncs.com/api/v1",
        session=session,
    )
    with pytest.raises(wan_image.WanImageError):
        adapter.save("https://127.0.0.1/private.png", tmp_path / "private.png")
    assert not session.gets


def test_existing_image_generator_can_select_wan(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "image_config", classmethod(lambda _: {
        "provider": "wan",
        "api_url": "https://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "api_key": "local-test-key",
        "model": "wan2.7-image",
        "size": "auto",
    }))
    monkeypatch.setattr(Config, "generation_config", classmethod(lambda _: {
        "retry_count": 0, "retry_interval_seconds": 1
    }))
    calls = []
    def fake_generate(self, prompt, size, output_path):
        calls.append((prompt, size))
        with open(output_path, "wb") as stream:
            stream.write(png_bytes())
        return output_path
    monkeypatch.setattr(wan_image.WanImageGenerator, "generate", fake_generate)
    monkeypatch.setattr(ImageGenerator, "_wait_for_rate_limit", lambda _: None)
    item = ImageGenerator(str(tmp_path)).generate(
        "panda holding a coin", width=1920, height=1080, style="3D动画"
    )
    assert isinstance(item, GeneratedImagePath)
    assert calls[0][1] == "1280*720"
    assert "panda holding a coin" in item.submitted_prompt
    assert Image.open(item).size == (32, 24)


class DummyPlanner:
    def __init__(self):
        self.calls = 0
    def _call_api(self, **kwargs):
        self.calls += 1
        return json.dumps({
            "visual_bible": {
                "character_lock": "One black-and-white panda in the same blue coat",
                "environment_lock": "A small mountain village",
                "art_direction": "Warm 3D animation",
                "camera_language": "Alternating wide and close shots",
                "negative_rules": "No overlay typography",
            },
            "shots": [
                {"index": 0, "image_prompt": "Same blue-coated panda on village bridge at sunrise, wide angle cinematic lighting", "motion_prompt": "熊猫站在村庄木桥上，保持蓝色外套、圆形眼睛和暖色三维动画画风，晨光从右侧照亮桥面；熊猫先低头看看手中的硬币，再缓缓抬头朝桥尾望去，右手轻轻握紧硬币，身体随呼吸自然起伏，衣角被微风吹动；[Push in] 镜头缓慢推进，沿原来的机位靠近主体，村庄背景逐渐柔化，晨光方向保持一致；结尾熊猫面向桥前方，右手停在胸前，服装与站位保持稳定，留下清晰的下一步行走姿态。", "camera": "wide shot", "start_state": "熊猫站在桥上", "end_state": "熊猫面向桥尾", "shot_type": "scene"},
                {"index": 1, "image_prompt": "Same blue-coated panda crossing the same bridge, close shot warm cinematic light", "motion_prompt": "熊猫从同一座村庄木桥的中央出发，蓝色外套、黑白毛色和晨光环境保持一致，右手握住硬币；熊猫先迈出左脚，再以轻快但自然的速度向桥尾走去，手臂轻轻摆动，硬币始终握在右手，外套衣摆随步伐晃动，动作不突然跳跃；[Tracking shot] 平稳跟拍，衣服不变，镜头从熊猫侧面缓慢移动，背景桥栏顺着行走方向掠过；结尾熊猫来到桥尾停下，右手举起硬币，目光转向村庄入口，画面稳定地保留角色姿态与下一镜头的接续方向。", "camera": "close side shot", "start_state": "熊猫从桥中央出发", "end_state": "熊猫在桥尾停下", "shot_type": "portrait"},
            ],
        })


def test_global_director_plans_once_and_persists_motion(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    planner = DummyPlanner()
    args = {
        "script": "甲乙", "segments": ["甲", "乙"], "summary": "熊猫成长故事",
        "style": "3D动画", "ratio": "16:9",
    }
    first = plan_storyboard("test-project", planner, **args)
    second = plan_storyboard("test-project", planner, **args)
    assert planner.calls == 1
    assert first["fingerprint"] == second["fingerprint"]
    assert "平稳跟拍，衣服不变" in lookup_shot("test-project", 1, "乙")["motion_prompt"]
    assert lookup_shot("test-project", 1, "变更后的旁白") == {}


def test_storyboard_failure_is_recoverable(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    class BrokenModel:
        def _call_api(self, **kwargs):
            return "not json"
    plan = plan_storyboard("broken", BrokenModel(),
        script="text", segments=["first"], summary="summary", style="movie", ratio="9:16"
    )
    assert plan["shots"] == {}
    assert load_plan("broken")["visual_bible"] == {}


def test_wan_preserves_receipt_and_stops_on_extra_images(tmp_path):
    import json
    class ExtraImages(FakeSession):
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return FakeResponse(data={"request_id": "test-request", "usage": {"image_count": 2},
                "output": {"choices": [{"message": {"content": [
                    {"image": "https://result.oss-cn-beijing.aliyuncs.com/a.png"},
                    {"image": "https://result.oss-cn-beijing.aliyuncs.com/b.png"}]}}]}})
    session = ExtraImages()
    adapter = wan_image.WanImageGenerator("test-only", "https://dashscope.aliyuncs.com", session=session)
    with pytest.raises(wan_image.WanImageError, match="张数异常"):
        adapter.generate("chickpeas", "1280*720", tmp_path / "shot.png")
    receipt = json.loads((tmp_path / "shot.receipt.json").read_text())
    assert receipt["returned_count"] == receipt["usage_image_count"] == 2
    assert len(session.posts) == 1
    assert not session.gets
