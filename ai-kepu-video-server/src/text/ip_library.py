"""Local cross-project IP library and immutable project-bound IP snapshots.

Every new video can bind an existing storytelling persona before storyboard
generation. Updating a library profile never silently changes past projects.
Reference pictures are stored as versioned, validated local images.
"""
import io
import json
import os
import re
import threading
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from src.config import Config
from src.media.voice_reference import validate_voice_sample


_LOCK = threading.RLock()
_FIELDS = ("name", "persona", "voice_style", "voice_type", "catchphrase", "canon", "visual_identity", "outfit",
           "world", "art_style", "camera_style", "negative_rules")
_MAX = {
    "name": 80, "persona": 900, "voice_style": 650, "voice_type": 120,
    "catchphrase": 200, "canon": 1200,
    "visual_identity": 1000, "outfit": 450, "world": 600,
    "art_style": 650, "camera_style": 400, "negative_rules": 400,
}


def _root():
    return Config.BASE_DIR / "data" / "media" / "_ip_library"


def _profiles_path():
    return _root() / "profiles.json"


def _project_path(task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(task_id)):
        raise ValueError("非法项目 ID")
    return _root() / "projects" / f"{task_id}.json"


def _load():
    path = _profiles_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        raise ValueError("IP 角色库数据文件损坏，请先恢复备份") from None


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def list_profiles():
    with _LOCK:
        return sorted(_load().values(), key=lambda p: p["name"].lower())


def get_profile(ip_id):
    with _LOCK:
        record = _load().get(str(ip_id))
        return dict(record) if record else None


def save_profile(data, ip_id=None):
    if not isinstance(data, dict):
        raise ValueError("IP 配置格式错误")
    with _LOCK:
        profiles = _load()
        if ip_id and ip_id not in profiles:
            raise KeyError(ip_id)
        ident = ip_id or uuid.uuid4().hex
        previous = profiles.get(ident) or {}
        profile = {key: str(data.get(key, previous.get(key, "")) or "").strip()[:_MAX[key]]
                   for key in _FIELDS}
        if not profile["name"]:
            raise ValueError("IP 名称不能为空")
        profile["ip_id"] = ident
        profile["version"] = int(previous.get("version") or 0) + 1
        profile["reference_image"] = previous.get("reference_image")
        profile["reference_voice"] = previous.get("reference_voice")
        profile["voice_reference_version"] = previous.get("voice_reference_version", 0)
        profiles[ident] = profile
        _write(_profiles_path(), profiles)
        return dict(profile)


def save_reference(ip_id, image_bytes):
    if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
        raise ValueError("参考图不能为空或大于 20MB")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("参考图仅支持 PNG/JPEG/WEBP")
            image.load()
            if not (240 <= image.width <= 8000 and 240 <= image.height <= 8000):
                raise ValueError("参考图边长需在 240–8000 像素")
            if not 1 / 8 <= image.width / image.height <= 8:
                raise ValueError("参考图宽高比例不符合要求")
            converted = image.convert("RGB")
            output = io.BytesIO()
            converted.save(output, "JPEG", quality=94)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("参考图格式损坏或不安全") from None
    with _LOCK:
        profiles = _load()
        if ip_id not in profiles:
            raise KeyError(ip_id)
        path = _root() / "references" / f"{ip_id}_{uuid.uuid4().hex}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(output.getvalue())
        updated = dict(profiles[ip_id])
        updated["reference_image"] = str(path)
        updated["version"] += 1
        profiles[ip_id] = updated
        _write(_profiles_path(), profiles)
        return dict(updated)


def save_voice_reference(ip_id, data):
    validate_voice_sample(data)
    with _LOCK:
        profiles = _load()
        if ip_id not in profiles:
            raise KeyError(ip_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(ip_id)):
            raise ValueError("非法 IP ID")
        path = Config.BASE_DIR / "data" / "_private" / "ip_voices" / str(ip_id) / f"{uuid.uuid4().hex}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        updated = dict(profiles[ip_id])
        updated["reference_voice"] = str(path)
        updated["voice_reference_version"] = int(updated.get("voice_reference_version") or 0) + 1
        updated["version"] += 1
        profiles[ip_id] = updated
        _write(_profiles_path(), profiles)
        return dict(updated)


def clear_voice_reference(ip_id):
    with _LOCK:
        profiles = _load()
        if ip_id not in profiles:
            raise KeyError(ip_id)
        updated = dict(profiles[ip_id])
        updated["reference_voice"] = None
        updated["voice_reference_version"] = int(updated.get("voice_reference_version") or 0) + 1
        updated["version"] += 1
        profiles[ip_id] = updated
        _write(_profiles_path(), profiles)
        # Versioned files stay available to old project snapshots.
        return dict(updated)


def public_profile(profile):
    if not profile:
        return None
    result = dict(profile)
    voice = result.pop("reference_voice", None)
    result["voice_reference_ready"] = bool(voice and Path(voice).is_file())
    return result


def bind_project(task_id, ip_id):
    """Snapshot profile+reference for this project, before the planner runs."""
    with _LOCK:
        profile = get_profile(ip_id)
        if not profile:
            raise KeyError(ip_id)
        path = _project_path(task_id)
        # A previously bound project may not change IP mid-generation.
        if path.exists():
            raise ValueError("项目已绑定 IP；请创建新项目以使用其他 IP")
        _write(path, profile)
        return profile


def project_ip(task_id):
    try:
        value = json.loads(_project_path(task_id).read_text("utf-8"))
        return value if isinstance(value, dict) and value.get("ip_id") else None
    except (OSError, ValueError):
        return None


def visual_bible(profile):
    if not profile:
        return {}
    return {
        "character_lock": "Fixed recurring IP: " + (
            str(profile.get("visual_identity") or profile.get("persona") or "")
            + "; fixed outfit: " + str(profile.get("outfit") or "")
        )[:800],
        "environment_lock": str(profile.get("world") or "")[:350],
        "art_direction": str(profile.get("art_style") or "")[:350],
        "camera_language": str(profile.get("camera_style") or "")[:350],
        "negative_rules": str(profile.get("negative_rules") or "")[:350],
    }


def narration_guidance(profile):
    if not profile:
        return ""
    return (
        "系列讲述者的固定 IP 设定，请保持叙事口吻，不要自称能够亲历未曾发生的事情：\n"
        f"名字：{profile.get('name', '')}\n"
        f"人设：{profile.get('persona', '')[:600]}\n"
        f"讲述口吻：{profile.get('voice_style', '')[:500]}\n"
        f"固定口头禅：{profile.get('catchphrase', '')[:150]}\n"
        f"系列不可篡改的背景事实：{profile.get('canon', '')[:900]}\n"
    )
