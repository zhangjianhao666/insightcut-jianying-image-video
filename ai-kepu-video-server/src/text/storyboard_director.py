"""Model-driven storyboard: one visual bible and coordinated shot prompts.

This is a planning artifact, not media generation. A model may suggest frames
and motion, but users approve the plan before paid images or clips are made.
"""
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path

from src.config import Config

log = logging.getLogger(__name__)
_PLAN_VERSION = 1


def enforce_visual_consistency(prompt: str, bible: dict) -> str:
    """Embed immutable art direction into every actual text-to-image request.

    A shared bible stored separately cannot affect the image model unless it
    is included in the prompt sent to that model.
    """
    if not isinstance(bible, dict) or not any(bible.values()):
        return str(prompt or "").strip()
    locks = [
        ("Recurring character identity and clothing", "character_lock"),
        ("Recurring environment and props", "environment_lock"),
        ("Art direction and color palette", "art_direction"),
        ("Avoid", "negative_rules"),
    ]
    details = [
        f"{label}: {str(bible[key])[:250]}"
        for label, key in locks if bible.get(key)
    ]
    full = str(prompt or "").strip()[:3000]
    if details:
        full += ". PROJECT-WIDE VISUAL CONTINUITY: " + "; ".join(details)
    return full[:4400]


def _location(task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(task_id)):
        raise ValueError("不合法的任务 ID")
    return Config.BASE_DIR / "data" / "media" / "_storyboard_plans" / f"{task_id}.json"


def _signature(script, segments, summary, style, ratio):
    payload = json.dumps(
        {"script": script, "segments": segments, "summary": summary,
         "style": style, "ratio": ratio, "version": _PLAN_VERSION},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_object(raw):
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text).strip()
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("分镜模型未返回 JSON 对象")
        result = json.loads(text[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError("分镜模型格式不正确")
    return result


def _normalize_plan(raw, count):
    bible = raw.get("visual_bible") or {}
    if not isinstance(bible, dict):
        bible = {}
    allowed_bible = ("character_lock", "environment_lock", "art_direction",
                     "camera_language", "negative_rules")
    clean_bible = {key: str(bible.get(key) or "")[:350] for key in allowed_bible}
    shots = {}
    for shot in raw.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        try:
            index = int(shot.get("index"))
        except (ValueError, TypeError):
            continue
        if not 0 <= index < count:
            continue
        image_prompt = str(shot.get("image_prompt") or "").strip()[:1800]
        motion_prompt = str(shot.get("motion_prompt") or "").strip()[:600]
        if len(image_prompt) < 10:
            continue
        shots[str(index)] = {
            "image_prompt": image_prompt,
            "motion_prompt": motion_prompt or "镜头缓慢推进，画面主体保持一致，环境产生轻微自然运动。",
            "shot_type": str(shot.get("shot_type") or "scene")[:50],
        }
    return {"visual_bible": clean_bible, "shots": shots}


def load_plan(task_id):
    path = _location(task_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == _PLAN_VERSION:
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return None


def lookup_shot(task_id, index, text=None):
    plan = load_plan(task_id)
    if not plan:
        return {}
    try:
        index = int(index)
    except (TypeError, ValueError):
        return {}
    # A resegmented or edited narration must not reuse an old motion prompt.
    old_segments = plan.get("segments") or []
    if text is not None and (index >= len(old_segments) or old_segments[index] != text):
        return {}
    return (plan.get("shots") or {}).get(str(index)) or {}


def _write(task_id, plan):
    path = _location(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def plan_storyboard(task_id, generator, *, script, segments, summary, style, ratio):
    """Build once per text/style revision. On model failure, caller falls back
    to individual image prompts. This must not erase existing shot assets.
    """
    if not segments:
        return {"visual_bible": {}, "shots": {}}
    fingerprint = _signature(script, segments, summary, style, ratio)
    previous = load_plan(task_id)
    if previous and previous.get("fingerprint") == fingerprint:
        return previous

    # For a long documentary, the model builds the shared bible only; the
    # existing per-shot queue remains responsible for unplanned shots.
    compact = len(segments) <= 12
    excerpt = [(i, text[:160]) for i, text in enumerate(segments)] if compact else (
        [(i, text[:120]) for i, text in enumerate(segments[:12])]
        + [(len(segments)-3+i, text[:120]) for i, text in enumerate(segments[-3:])]
    )
    instructions = (
        "你是长视频分镜导演与美术指导。综合全文规划连续镜头，不要把每句旁白逐字画出来。"
        "主角、服饰、道具、环境在镜头间保持一致；画面构图要有远景中景特写的变化。"
        "旁白中的数据、数字或术语优先用物体和情景隐喻表现，不要在生成图片上写数字和字幕。"
        "返回一个 JSON 对象，必须包含 visual_bible 字段："
        "character_lock（固定人物外观或无人物说明）、environment_lock、art_direction、camera_language、negative_rules。"
        "另外返回 shots 数组，每项包含 index（0开始）、image_prompt（英文、明确主体场景构图光线和动作的 35-80 词画面提示词）、"
        "motion_prompt（中文、画面动作及运镜、不添加说话或文字）、shot_type（scene/portrait/diagram/transition）。"
        "各镜头服从 visual_bible，不允许漂移。只输出 JSON。"
    )
    if not compact:
        instructions += "当前镜头太多，本次只创建全片 visual_bible；shots 返回空数组。"
    payload = (
        f"{instructions}\n画风：{style[:250]}\n画幅：{ratio}\n全文总结：{summary[:650]}"
        f"\n分镜编号与对应旁白：{json.dumps(excerpt, ensure_ascii=False)}"
    )
    try:
        raw = generator._call_api(
            messages=[{"role": "user", "content": payload}],
            max_tokens=3800 if compact else 850,
        )
        parsed = _normalize_plan(_json_object(raw), len(segments))
    except Exception:
        # Preserve original reliable generation path on model/JSON failure.
        log.warning("全片分镜导演规划失败；继续使用逐分镜提示词生成")
        parsed = {"visual_bible": {}, "shots": {}}
    record = {
        "version": _PLAN_VERSION, "fingerprint": fingerprint,
        "segments": list(segments), "visual_bible": parsed["visual_bible"],
        "shots": parsed["shots"],
    }
    _write(task_id, record)
    return record
