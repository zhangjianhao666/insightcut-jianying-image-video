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
from src.text.continuity import normalize_boundaries, normalize_shot, audit_plan

log = logging.getLogger(__name__)
_PLAN_VERSION = 4
# Shared fallback so a shot without a planned motion script still tells the
# image-to-video model to continue the previous frame instead of starting over.
DEFAULT_MOTION_PROMPT = (
    "正常切镜：这是独立构图，沿用已经确认的角色外观、服装和画风；"
    "主体先有轻微自然动作（呼吸、转头、抬手），环境有细微动态（光影变化、"
    "背景物体自然运动），[Push in] 镜头平稳缓慢推进，不改变角色外貌与画面布局，"
    "不出现文字或字幕；结尾画面保持清晰稳定，"
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
            **normalize_shot(shot, index),
        }
    return {"visual_bible": clean_bible, "shots": normalize_boundaries(shots)}


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
        "你是专业动画导演和剪辑师。把一集故事规划为可独立生成、后期能剪辑的多个短镜头。"
        "不要假定每个镜头的尾帧必须等于下一镜头的首帧：正常切镜可以从全景切特写、反打镜头或跨场景；"
        "只有标记为 continue 的同场景连续动作才要求动作方向、姿态和站位接续。"
        "match_cut 则只匹配视觉元素或动作方向，不要求两帧像素相同。"
        "全片固定角色长相、服装、色彩体系和摄影风格；场景变化必须是剧情需要，不要无故改变道具和角色。"
        "返回纯 JSON 对象：visual_bible(character_lock, environment_lock, art_direction,"
        " camera_language, negative_rules) 和 shots 列表。"
        "shots 每项必须包含：index(0开始)、scene_id(同一实际场景用同一编号，如 scene_01)、"
        "character_ids(角色ID数组)、location_id(固定场景/房间标识)、"
        "camera(景别/机位/轴线)、start_state(该镜头开场角色位置/手部/视线/道具状态)、"
        "end_state(镜头结束状态)、transition_to_next(cut/match_cut/continue 之一)、"
        "shot_type(scene/portrait/diagram/transition)、"
        "image_prompt(英文35-80词：主体/景别/机位/光线/角色标识)、"
        "motion_prompt(中文150-400字：主角动作与运镜、光影和环境，不含口播台词)。"
        "motion_prompt 分四段用分号隔开：① 开场状态与视觉约束；② 动作顺序与速度；"
        "③ [Push in]、[Pan left]、[Tracking shot] 或 [Static shot] 等运镜；"
        "④ 本镜头收尾姿态。不要无条件写承接上一镜头尾帧。"
        "最后一镜头 transition_to_next 必须是 cut。严格只输出 JSON。"
    )
    from src.media.minimax_i2v import is_h3_model
    video = Config.load_model_config().get("video") or {}
    if video.get("provider") == "minimax" and is_h3_model(video.get("model")):
        instructions += (
            "本期采用 H3 原生中文讲解，每个分镜由同一 IP 主持人说出对应原文。"
            "动作脚本描述自然口型、表演和视线，不得禁止对白、讲话或口型；"
            "准确台词由系统从分镜原文编译，声音参考由系统绑定，不要改写台词或生成字幕。"
        )
    else:
        instructions += "有台词时描述表演与视线即可，讲述音色由独立音轨控制，不要求视频模型生成对白。"
    if not compact:
        instructions += "当前镜头太多，本次只创建全片 visual_bible；shots 返回空数组。"
    ip_prompt = ""
    if ip_profile:
        ip_prompt = (
            "\n固定系列 IP（优先级高于单集发挥，不允许改变角色外观）："
            + json.dumps({
                key: ip_profile.get(key) for key in (
                    "name", "persona", "voice_style", "catchphrase", "canon", "visual_identity", "outfit",
                    "world", "art_style", "camera_style", "negative_rules"
                )
            }, ensure_ascii=False)[:2000]
            + "\n请让这一 IP 作为本集的稳定讲述者或画面主角。"
        )
    payload = (
        f"{instructions}\n画风：{style[:250]}\n画幅：{ratio}\n全文总结：{summary[:650]}"
        f"\n分镜编号与对应旁白：{json.dumps(excerpt, ensure_ascii=False)}"
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
    record["continuity_warnings"] = audit_plan(record)
    _write(task_id, record)
    return record
