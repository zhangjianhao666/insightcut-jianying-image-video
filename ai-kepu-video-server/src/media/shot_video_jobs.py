"""Per-shot optional video generation with local persisted checkpoints.

A matching completed clip is used in exports; if the source image changes,
it is ignored automatically and the existing image-motion renderer remains
the fallback. An interrupted paid task is polled, never silently resubmitted.
"""
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path

from src.config import Config
from src.media.minimax_i2v import MinimaxImageToVideo, MinimaxVideoError
from src.media.wan_i2v import WanImageToVideo, WanVideoError


logger = logging.getLogger(__name__)

_active = set()
_lock = threading.RLock()
_slots = threading.BoundedSemaphore(2)


def _video_config():
    return Config.load_model_config().get("video") or {}


def video_provider():
    """Configured image-to-video provider; Wan stays the default."""
    return "minimax" if str(_video_config().get("provider") or "").strip().lower() == "minimax" else "wan"


def provider_class():
    return MinimaxImageToVideo if video_provider() == "minimax" else WanImageToVideo


def duration_options():
    """Clip lengths the configured provider accepts right now."""
    video = _video_config()
    return tuple(provider_class().duration_options(video.get("resolution"), video.get("model")))


def supports_audio():
    """Whether the configured provider returns clips with their own audio."""
    return bool(provider_class().supports_audio(_video_config().get("model")))


def default_duration():
    options = duration_options()
    configured = _video_config().get("duration")
    return configured if configured in options else options[0]


def _failure_reason(exc):
    """Keep the provider's sanitised reason instead of hiding it.

    Adapters already strip credentials and raw payloads, so their message is
    safe to show. Anything else keeps a short class name plus a stable hint.
    """
    if isinstance(exc, (MinimaxVideoError, WanVideoError)):
        return str(exc).strip() or "视频生成失败，请稍后重试"
    return (
        "视频生成失败，请检查日志、网络或 API 额度；原任务 ID 已保留"
        f"（{type(exc).__name__}）"
    )


def _has_audio_track(path):
    """True when a generated clip carries its own audio (MiniMax-H3 does)."""
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not Path(path).is_file():
        return False
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "audio" in result.stdout


def _identity(image_path):
    path = Path(image_path).resolve()
    stat = path.stat()
    return hashlib.sha256(
        f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()


def _directory(task_id, base=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(task_id)):
        raise ValueError("非法任务 ID")
    return (Path(base) if base else Config.BASE_DIR / "data" / "media" / "_shot_videos") / str(task_id)


def _state_file(task_id, segment_index, base=None):
    if not isinstance(segment_index, int) or segment_index < 0 or segment_index > 10000:
        raise ValueError("非法分镜序号")
    return _directory(task_id, base) / f"{segment_index:04d}.json"


def _read(task_id, segment_index, base=None):
    path = _state_file(task_id, segment_index, base)
    if not path.exists():
        return {"status": "not_generated"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "failed", "error": "分镜视频记录损坏"}


def _write(task_id, segment_index, record, base=None):
    path = _state_file(task_id, segment_index, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def status(task_id, segment_index, image_path=None, base=None):
    record = _read(task_id, segment_index, base)
    if image_path:
        try:
            if record.get("image_signature") != _identity(image_path):
                return {"status": "stale" if record.get("image_signature") else "not_generated"}
        except OSError:
            return {"status": "not_generated"}
    key = (str(task_id), segment_index)
    if record.get("status") == "running" and key not in _active:
        return {**record, "status": "interrupted"}
    if record.get("status") == "completed" and not Path(record.get("video_path") or "").is_file():
        return {**record, "status": "failed", "error": "本地视频文件丢失"}
    return record


def matching_video_path(task_id, segment):
    try:
        index = int(segment.get("segment_index"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not segment.get("image_path"):
        return None
    item = status(task_id, index, segment.get("image_path"))
    if item.get("status") == "completed":
        # Legacy exports also need reference-aware invalidation; otherwise
        # they may reuse a previously approved clip with stale scene/voice.
        from src.media import h3_references
        if h3_references.mode(task_id) == "reference":
            from src.media.minimax_i2v import is_h3_model
            if video_provider() != "minimax" or not is_h3_model(_video_config().get("model")):
                return None
            from src.text.storyboard_director import load_plan
            shot = ((load_plan(task_id) or {}).get("shots") or {}).get(str(index)) or {}
            try:
                expected = h3_references.fingerprint_now(
                    task_id, segment["image_path"], shot.get("scene_id") or "",
                )
            except (OSError, ValueError):
                return None
            if expected != item.get("reference_fingerprint", "first_frame"):
                return None
        elif item.get("reference_fingerprint", "first_frame") != "first_frame":
            return None
        return item.get("video_path")
    return None


def start(task_id, segment_index, image_path, prompt, *, duration=None, force_new=False, base=None, provider_factory=None, reference_assets=None):
    """Explicit user action only. Returns immediately; no paid batch dispatch."""
    image_path = str(Path(image_path).resolve())
    signature = _identity(image_path)
    if not str(prompt).strip() or len(str(prompt)) > 1800:
        raise ValueError("请输入不超过 1800 字的视频动作提示词")
    duration = default_duration() if duration is None else duration
    options = duration_options()
    if duration not in options:
        raise ValueError("生成时长应为 " + "、".join(str(value) for value in options) + " 秒")
    reference_fingerprint = (
        str(reference_assets.get("fingerprint") or "")
        if reference_assets is not None else "first_frame"
    )
    if reference_assets is not None:
        from src.media.minimax_i2v import is_h3_model
        video = _video_config()
        if video_provider() != "minimax" or not is_h3_model(video.get("model")):
            raise ValueError("H3 多模态参考模式需要选择 MiniMax-H3 或 MiniMax-H3-Max")
        if not reference_fingerprint:
            raise ValueError("H3 参考素材快照缺少指纹")
    key = (str(task_id), segment_index)
    with _lock:
        if key in _active:
            return status(task_id, segment_index, image_path, base)
        existing = status(task_id, segment_index, image_path, base)
        same_request = (
            existing.get("image_signature") == signature
            and existing.get("prompt") == prompt
            and existing.get("duration") == duration
            and existing.get("reference_fingerprint", "first_frame") == reference_fingerprint
        )
        if not force_new and same_request and existing.get("status") == "completed":
            return existing
        # Constructing the adapter checks credentials before creating any job.
        adapter_class = provider_factory or provider_class()
        provider = adapter_class()
        reuse_id = (
            existing.get("provider_task_id") if same_request and not force_new
            and existing.get("status") in {"interrupted", "failed"} else None
        )
        directory = _directory(task_id, base)
        record = {
            "status": "running", "image_signature": signature,
            "prompt": prompt, "duration": duration,
            "reference_fingerprint": reference_fingerprint,
            "provider_task_id": reuse_id,
            "video_path": str(directory / f"{segment_index:04d}_{uuid.uuid4().hex}.mp4"),
        }
        _write(task_id, segment_index, record, base)
        _active.add(key)

    def work():
        with _slots:
            try:
                if record["provider_task_id"]:
                    task = record["provider_task_id"]
                elif reference_assets is not None:
                    task = provider.submit(
                        image_path, prompt, duration=duration,
                        reference_assets=reference_assets,
                    )
                else:
                    task = provider.submit(image_path, prompt, duration=duration)
                record["provider_task_id"] = task
                with _lock:
                    _write(task_id, segment_index, record, base)
                url = provider.wait(task)
                provider.download(url, record["video_path"])
                record["has_audio"] = _has_audio_track(record["video_path"])
                record["status"] = "completed"
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = _failure_reason(exc)
                logger.warning(
                    "shot video %s#%s failed: %s", task_id, segment_index, exc,
                    exc_info=True,
                )
            finally:
                with _lock:
                    # Do not overwrite a newer request's record.
                    if _read(task_id, segment_index, base).get("video_path") == record["video_path"]:
                        _write(task_id, segment_index, record, base)
                    _active.discard(key)

    thread = threading.Thread(target=work, daemon=True, name=f"shot-i2v-{segment_index}")
    thread.start()
    return {"status": "running", "provider_task_id": reuse_id, "duration": duration}
