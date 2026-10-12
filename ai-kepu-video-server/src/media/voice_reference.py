"""Shared bounded PCM WAV validation for private native voice references."""
import io
import wave


def validate_voice_sample(data):
    if not isinstance(data, bytes) or not data or len(data) > 8 * 1024 * 1024:
        raise ValueError("声音参考 WAV 不得超过 8 MB")
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            if handle.getcomptype() != "NONE" or handle.getnchannels() not in {1, 2}:
                raise ValueError("仅支持单/双声道未压缩 PCM WAV")
            if handle.getframerate() <= 0 or handle.getsampwidth() not in {1, 2, 3, 4}:
                raise ValueError("WAV 参数不支持")
            seconds = handle.getnframes() / handle.getframerate()
            if not 2 <= seconds <= 15:
                raise ValueError("声音样本时长须为 2–15 秒")
            expected = handle.getnframes() * handle.getnchannels() * handle.getsampwidth()
            if len(handle.readframes(handle.getnframes())) != expected:
                raise ValueError("WAV 音频内容不完整")
    except (wave.Error, EOFError, OSError):
        raise ValueError("无效的 PCM WAV 音频") from None
    return seconds
