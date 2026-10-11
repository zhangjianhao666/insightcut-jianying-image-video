"""The Alibaba workspace probe is network-free under these tests."""
from unittest.mock import patch

import pytest

from scripts import probe_bailian
from src.media.wan_i2v import WanImageToVideo


def test_workspace_api_paths():
    base = "https://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    assert probe_bailian.endpoints(base) == (
        "https://demo.cn-beijing.maas.aliyuncs.com/api/v1", base
    )


@pytest.mark.parametrize("bad", [
    "http://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "https://malicious.example.com/compatible-mode/v1",
    "https://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
])
def test_workspace_probe_rejects_unsafe_endpoint(bad):
    with pytest.raises(ValueError):
        probe_bailian.endpoints(bad)


def test_no_paid_completion_without_chat_flag(capsys):
    calls = []
    def fake_request(method, url, key, payload=None):
        calls.append(method)
        return 200, {"output": {"models": [{"model": "qwen3.8-flash"}], "total": 1}}
    env = {"DASHSCOPE_API_KEY": "test-only", "LLM_BASE_URL": "https://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"}
    with patch.dict("os.environ", env), patch.object(probe_bailian, "request_json", side_effect=fake_request):
        assert probe_bailian.main([]) == 0
    assert calls == ["GET"]
    assert "test-only" not in capsys.readouterr().out


def test_one_explicit_chat_request(capsys):
    calls = []
    def fake_request(method, url, key, payload=None):
        calls.append((method, payload))
        if method == "POST":
            return 200, {"choices": [{"message": {"content": "OK"}}], "usage": {"total_tokens": 10}}
        return 200, {"output": {"models": [], "total": 0}}
    env = {"DASHSCOPE_API_KEY": "test-only", "LLM_BASE_URL": "https://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"}
    with patch.dict("os.environ", env), patch.object(probe_bailian, "request_json", side_effect=fake_request):
        assert probe_bailian.main(["--chat"]) == 0
    assert [item[0] for item in calls] == ["GET", "POST"]
    assert calls[-1][1]["max_tokens"] == 24
    assert "test-only" not in capsys.readouterr().out


def test_wan_uses_workspace_video_endpoint_when_chat_base_is_configured():
    base = "https://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    with patch.dict("os.environ", {"LLM_BASE_URL": base, "DASHSCOPE_BASE_URL": ""}):
        adapter = WanImageToVideo(api_key="test-only")
    assert adapter.base_url == "https://demo.cn-beijing.maas.aliyuncs.com/api/v1"
