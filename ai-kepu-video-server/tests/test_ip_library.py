"""IP narration and reference-image smoke tests. Never issue network requests."""
import io
import json
from pathlib import Path

from PIL import Image
import pytest

from src.config import Config
from src.media.wan_image import WanImageGenerator
from src.text import ip_library
from src.text.storyboard_director import plan_storyboard, enforce_visual_consistency


def make_jpeg():
    output = io.BytesIO()
    Image.new("RGB", (320, 320), (30, 60, 150)).save(output, "JPEG")
    return output.getvalue()


@pytest.fixture
def local_ip_env(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)
    return tmp_path


def test_ip_can_be_shared_across_episodes_and_older_snapshot_is_immutable(local_ip_env):
    profile = ip_library.save_profile({
        "name": "蓝衣熊猫", "visual_identity": "Black and white panda, round face",
        "outfit": "Always wears the same blue coat",
        "persona": "A friendly financial storyteller",
        "voice_style": "Warm, gentle, first-person narration",
        "voice_type": "mimo:冰糖",
    })
    first = ip_library.bind_project("episode-001", profile["ip_id"])
    assert first["voice_type"] == "mimo:冰糖"
    modified = ip_library.save_profile(
        {"name": "蓝衣熊猫", "outfit": "Different red coat"}, profile["ip_id"]
    )
    second = ip_library.bind_project("episode-002", profile["ip_id"])
    assert modified["version"] == profile["version"] + 1
    assert ip_library.project_ip("episode-001")["outfit"] == "Always wears the same blue coat"
    assert ip_library.project_ip("episode-002")["outfit"] == "Different red coat"
    assert second["ip_id"] == first["ip_id"]
    with pytest.raises(ValueError):
        ip_library.bind_project("episode-001", profile["ip_id"])


def test_reference_is_stored_as_new_version_and_bad_upload_rejected(local_ip_env):
    profile = ip_library.save_profile({"name": "固定角色"})
    with pytest.raises(ValueError):
        ip_library.save_reference(profile["ip_id"], b"not-image")
    updated = ip_library.save_reference(profile["ip_id"], make_jpeg())
    assert Path(updated["reference_image"]).is_file()
    assert Image.open(updated["reference_image"]).size == (320, 320)
    pinned = ip_library.bind_project("series1", profile["ip_id"])
    ip_library.save_reference(profile["ip_id"], make_jpeg())
    assert ip_library.project_ip("series1")["reference_image"] == pinned["reference_image"]


class FakeResponse:
    def raise_for_status(self):
        pass
    def json(self):
        return {"output": {"choices": [{"message": {"content": [
            {"image": "https://example.oss-cn-beijing.aliyuncs.com/scene.png"}
        ]}}]}}


class ImageSession:
    def __init__(self):
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse()


def test_wan_image_request_includes_reference_data_not_local_path(local_ip_env):
    profile = ip_library.save_profile({"name": "熊猫IP"})
    profile = ip_library.save_reference(profile["ip_id"], make_jpeg())
    session = ImageSession()
    adapter = WanImageGenerator(
        api_key="test-token",
        base_url="https://demo.cn-beijing.maas.aliyuncs.com/api/v1",
        session=session,
    )
    url = adapter.submit("Same recurring panda in a mountain village", "1280*720", profile["reference_image"])
    assert url.endswith(".png")
    content = session.calls[0]["json"]["input"]["messages"][0]["content"]
    assert content[1]["image"].startswith("data:image/jpeg;base64,")
    assert str(local_ip_env) not in json.dumps(content)
    assert "锁定脸型" in content[0]["text"]


def test_model_storyboard_ip_wins_over_new_episode_style(local_ip_env):
    profile = ip_library.save_profile({
        "name": "阿财", "visual_identity": "Small panda round eyes",
        "outfit": "Fixed bright-blue waistcoat",
        "world": "Cheerful village library",
        "art_style": "Soft 3D animation",
    })
    ip_library.bind_project("series-episode", profile["ip_id"])
    class Planner:
        def _call_api(self, **kwargs):
            return json.dumps({
                "visual_bible": {"character_lock": "Wrong completely different character"},
                "shots": [{"index": 0, "image_prompt": "A character tells a dramatic story in a village at sunrise", "motion_prompt": "小熊猫站在图书馆的木桌旁，保持圆眼睛、亮蓝色马甲和柔和三维动画画风，桌上书本与窗边晨光保持一致；小熊猫先将右手放在书页上，再缓缓抬头看向镜头，左手轻轻指向插画，身体自然转向观众，动作按顺序展开，马甲和面部外形不发生变化；[Push in] 镜头从中景平稳推进到角色半身构图，保持原来的轴线与柔和光线，背景书架逐渐虚化但物品位置不改变；收尾小熊猫右手停在书页上，左手自然放下，目光平静地面向观众，留下稳定姿态与温暖的场景氛围。", "camera": "medium shot", "start_state": "小熊猫站在桌旁", "end_state": "小熊猫面向观众", "shot_type": "scene"}],
            })
    plan = plan_storyboard(
        "series-episode", Planner(), script="示例",
        segments=["示例"], summary="奇遇", style="Anime", ratio="16:9",
    )
    assert "Fixed bright-blue waistcoat" in plan["visual_bible"]["character_lock"]
    full_prompt = enforce_visual_consistency(
        plan["shots"]["0"]["image_prompt"], plan["visual_bible"]
    )
    assert "Fixed bright-blue waistcoat" in full_prompt
