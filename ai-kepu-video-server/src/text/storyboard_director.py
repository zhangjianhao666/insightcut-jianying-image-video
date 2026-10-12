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
_PLAN_VERSION = 2
# Shared fallback so a shot without a planned motion script still tells the
# image-to-video model to continue the previous frame instead of starting over.
DEFAULT_MOTION_PROMPT = (
    "承接上一镜头尾帧：同一人物外观、服装、场景、道具与光线色调保持一致；"
    "主体先有轻微自然动作（呼吸、转头、抬手），环境有细微动态（光影变化、"
    "背景物体自然运动），[Push in] 镜头平稳缓慢推进，不改变角色外貌与画面布局，"
    "不出现文字、字幕或口型说话；结尾画面停在主体清晰、构图稳定的位置，"
    "便于下一镜头无缝衔接。"
)


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
        motion_prompt = str(shot.get("motion_prompt") or "").strip()[:1800]
        if len(image_prompt) < 10:
            continue
        shots[str(index)] = {
            "image_prompt": image_prompt,
            "motion_prompt": motion_prompt or DEFAULT_MOTION_PROMPT,
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
    from src.text.ip_library import project_ip, visual_bible
    ip_profile = project_ip(task_id)
    ip_bible = visual_bible(ip_profile)
    profile_fingerprint = (
        str((ip_profile or {}).get("ip_id") or "")
        + ":" + str((ip_profile or {}).get("version") or "")
    )
    fingerprint = _signature(script, segments, summary, style + profile_fingerprint, ratio)
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
        "你是长视频分镜导演与美术指导。你要把整集规划成一条首尾相接的连续镜头序列："
        "上一镜头的结束画面就是下一镜头的开始画面，相邻镜头必须能从提示词层面无缝衔接，"
        "不要把每句旁白逐字画出来。"
        "主角、服饰、道具、环境、光线与色调在镜头间保持一致，只允许景别、机位和主体动作推进；"
        "画面构图要有远景中景特写的变化。"
        "旁白中的数据、数字或术语优先用物体和情景隐喻表现，不要在生成图片上写数字和字幕。"
        "返回一个 JSON 对象，必须包含 visual_bible 字段："
        "character_lock（固定人物外观或无人物说明）、environment_lock、art_direction、camera_language、negative_rules。"
        "另外返回 shots 数组，每项包含 index（0开始）、shot_type（scene/portrait/diagram/transition）、"
        "image_prompt（英文、明确主体场景构图光线和动作的 35-80 词画面提示词）、"
        "motion_prompt（中文 150-400 字，给图生视频模型使用的连续镜头脚本）。"
        "motion_prompt 必须写全四段并用分号分隔："
        "① 承接：本镜头开场如何延续上一镜头的尾帧——同一人物外观与服装、同一场景道具、同一光线色调；"
        "第一个镜头则描述起始画面；"
        "② 动作：主体按先后顺序做什么、速度与幅度，可写环境音效与氛围（雨声、脚步声、风声、背景音乐情绪），"
        "但禁止写台词、口型说话或任何屏幕文字；"
        "③ 运镜：在句中用官方英文方括号指令描述机位运动，可选 "
        "[Truck left] [Truck right] [Pan left] [Pan right] [Push in] [Pull out] "
        "[Pedestal up] [Pedestal down] [Tilt up] [Tilt down] [Zoom in] [Zoom out] "
        "[Tracking shot] [Static shot]；"
        "④ 收尾：镜头结束时画面停在哪里、留给下一镜头什么接续点。"
        "各镜头服从 visual_bible，不允许漂移。只输出 JSON。"
    )
    if not compact:
        instructions += "当前镜头太多，本次只创建全片 visual_bible；shots 返回空数组。"
    ip_prompt = ""
    if ip_profile:
        ip_prompt = (
            "\\n固定系列 IP（优先级高于单集发挥，不允许改变角色外观）："
            + json.dumps({
                key: ip_profile.get(key) for key in (
                    "name", "persona", "voice_style", "catchphrase", "canon", "visual_identity", "outfit",
                    "world", "art_style", "camera_style", "negative_rules"
                )
            }, ensure_ascii=False)[:2000]
            + "\\n请让这一 IP 作为本集的稳定讲述者或画面主角。"
        )
    payload = (
        f"{instructions}\\n画风：{style[:250]}\\n画幅：{ratio}\\n全文总结：{summary[:650]}"
        f"\\n分镜编号与对应旁白：{json.dumps(excerpt, ensure_ascii=False)}"
        + ip_prompt
    )
    try:
        raw = generator._call_api(
            messages=[{"role": "user", "content": payload}],
            # Detailed per-shot motion scripts need much more room than the
            # old one-line prompts; scale with the shot count.
            max_tokens=min(8000, max(4000, 600 * min(len(segments), 12))) if compact else 850,
        )
        parsed = _normalize_plan(_json_object(raw), len(segments))
    except Exception:
        # Preserve original reliable generation path on model/JSON failure.
        log.warning("全片分镜导演规划失败；继续使用逐分镜提示词生成")
        parsed = {"visual_bible": {}, "shots": {}}
    if ip_bible:
        # Series identity always overrides per-episode model improvisation.
        parsed["visual_bible"] = {
            **parsed["visual_bible"], **{k: v for k, v in ip_bible.items() if v}
        }
    record = {
        "version": _PLAN_VERSION, "fingerprint": fingerprint,
        "ip_id": (ip_profile or {}).get("ip_id"),
        "segments": list(segments), "visual_bible": parsed["visual_bible"],
        "shots": parsed["shots"],
    }
    _write(task_id, record)
    return record
