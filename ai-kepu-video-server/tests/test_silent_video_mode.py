"""No voice-provider credentials, requests or billing in silent mode tests."""
import asyncio
import wave

from src.draft.voice_catalog import parse_voice_key
from src.draft.voiceover import VoiceOverGenerator


def test_silent_voice_key_is_canonical():
    voice = parse_voice_key("silent:default")
    assert voice.provider == "silent"
    assert voice.key == "silent:default"


def test_silent_voice_generates_valid_wav_without_tts_key(tmp_path, monkeypatch):
    # An accidental external provider call should fail this test immediately.
    def fail(*args, **kwargs):
        raise AssertionError("No external TTS provider may be called")
    monkeypatch.setattr("src.draft.voiceover.requests.post", fail)
    generator = VoiceOverGenerator(
        output_dir=str(tmp_path),
        tts_config={
            "provider": "doubao", "api_key": "",
            "appid": "", "token": "", "cluster": "volcano_tts",
        },
    )
    output = generator.generate(
        "小狗钱钱提醒吉娅：先把梦想写下来，然后一点一点存钱。",
        filename="scene_001", voice_type="silent:default",
    )
    with wave.open(output, "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert 3.0 <= wav.getnframes() / wav.getframerate() <= 15.0
        assert wav.readframes(2500) == bytes(5000)


def test_silent_voice_in_public_catalog(monkeypatch):
    from src.api import routes
    monkeypatch.setattr(routes.mysql_client, "list_tts_voices", lambda **kwargs: [])
    class EmptyClones:
        def list(self, **kwargs):
            return []
    monkeypatch.setattr(routes, "_voice_clone_store", lambda: EmptyClones())
    monkeypatch.setattr(routes.Config, "tts_config", classmethod(
        lambda cls: {"allow_silent_mode": True}
    ))
    voices = asyncio.run(routes.get_voices(provider=None, include_disabled=True))
    voice = next(item for item in voices if item["id"] == "silent:default")
    assert voice["provider"] == "silent"
    assert voice["source"] == "local"
    assert voice["is_enabled"] is True


def test_silent_voice_skips_missing_provider_credentials(monkeypatch):
    from src.api import routes
    config = {
        "llm": {
            "provider": "custom",
            "protocol": "openai",
            "base_url": "https://example.invalid/v1",
            "api_key": "test-only",
            "model": "qwen3.8-flash",
        },
        "image": {
            "provider": "wan",
            "api_url": "https://example.invalid/api/v1",
            "api_key": "test-only",
            "model": "wan2.7-image",
        },
        "tts": {
            "provider": "doubao",
            "enabled_providers": ["doubao"],
            "auth_method": "access_token",
            "api_url": "https://example.invalid/tts",
            "appid": "", "token": "",
        },
    }
    monkeypatch.setattr(routes.Config, "load_model_config", lambda: config)
    result = routes._config_readiness("silent:default", "assets")
    assert result["status"] == "ready"
    assert next(item for item in result["items"] if item["key"] == "tts")["provider"] == "silent"
