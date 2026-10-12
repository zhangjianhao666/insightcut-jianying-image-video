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


def enforce_visual_consistency(prompt: str, bible: dict, shot=None) -> str:
    from src.text.image_contract import compile_shot
    if not bible and not shot:
        return str(prompt or "").strip()
    return compile_shot(prompt, bible, shot=shot)["prompt"]


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
    clean_bible = {key: bible.get(key) or "" for key in allowed_bible}
    if any(len(json.dumps(value, ensure_ascii=False)) > 5000 for value in clean_bible.values()):
        raise ValueError("完整视觉约束过长，请导演精简，不得截断")
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
        image_prompt = str(shot.get("image_prompt") or "").strip()
        motion_prompt = str(shot.get("motion_prompt") or "").strip()[:1800]
        if len(image_prompt) > 1800:
            raise ValueError("图片描述过长，请导演精简，禁止截断")
        if len(image_prompt) < 10:
            continue
        shots[str(index)] = {
            "image_prompt": image_prompt,
            "motion_prompt": motion_prompt or DEFAULT_MOTION_PROMPT,
            **({"duration": shot["duration"]} if type(shot.get("duration")) is int and 4 <= shot["duration"] <= 15 else {}),
            "purpose": str(shot.get("purpose") or "")[:400],
            "visual_contract": shot.get("visual_contract") or {},
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
    if not plan or plan.get("status") == "failed":
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


def record_planning_failure(task_id, *, stage, category, segments):
    """Preserve prior plan details while making explicit replan failure visible."""
    record = load_plan(task_id) or {"version": _PLAN_VERSION, "shots": {}, "visual_bible": {}}
    record.update(status="failed", error={"stage": stage, "category": category}, segments=list(segments))
    _write(task_id, record)
    log.warning("[%s] 分镜导演规划失败 stage=%s category=%s", task_id, stage, category)


def plan_storyboard(task_id, generator, *, script, segments, summary, style, ratio, force=False, outline=None, target_duration=None):
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
    saved = load_plan(task_id) or {}
    if outline is None and saved.get("segments") == list(segments):
        outline = saved.get("outline")
        old_shots = saved.get("shots") or {}
        if not outline and len(old_shots) == len(segments) and all(type(v.get("duration")) is int and 4 <= v["duration"] <= 15 for v in old_shots.values()):
            outline = [{"text": text, "duration": old_shots[str(i)]["duration"],
                        "purpose": old_shots[str(i)].get("purpose") or old_shots[str(i)].get("camera") or ""}
                       for i, text in enumerate(segments)]
    fingerprint = _signature(script, segments, summary, style + profile_fingerprint + (json.dumps(outline, ensure_ascii=False) if outline else "") + (str(target_duration) if target_duration else ""), ratio)
    previous = load_plan(task_id)
    if (not force and previous and previous.get("fingerprint") == fingerprint
            and previous.get("status") != "failed"
            and (len(segments) > 12 or set(previous.get("shots") or {}) ==
                 {str(i) for i in range(len(segments))})):
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
        "visual_contract(subject_mode:character/hands/object/diagram, framing:wide/medium/close_up/extreme_close_up/top_down, text_policy:forbidden)。"
        "纯食材特写使用object，不要无条件把主持人塞进每个画面；仅出现手时用hands，图解用diagram。"
        "画面描述必须具体可画，抽象营养概念不得直接变成含文字标签的伪科学图。"
        "每项只描述一个可独立生成的连续画面；禁止在单镜内拼图、分屏或多个并列画面，多个做法分到不同镜头。"
        "image_prompt(英文35-60词：主体/景别/机位/光线/角色标识)、"
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
        + (f"\nshots 每镜还必须包含duration（4至15秒整数），全部duration之和必须是{target_duration}秒。" if target_duration else "")
        + ("\n导演已决定的逐镜时长与意图（必须遵守，不改台词）：" + json.dumps(outline, ensure_ascii=False) if outline else "")
    )
    error = None
    stage = "provider"
    validation_category = "incomplete_shots"
    try:
        if ip_profile and ip_profile.get("reference_image") and not (ip_profile.get("visual_identity") and ip_profile.get("outfit")):
            stage = "validation"
            validation_category = "reference_identity_missing"
            raise ValueError("IP参考图已绑定，但外观和服装文字约束不完整")
        raw = generator._call_api(
            messages=[{"role": "user", "content": payload}],
            # The budget includes reasoning as well as JSON output. The current
            # DeepSeek model exhausted 4000 tokens before completing two shots.
            # Keep room for detailed motion scripts and scale with shot count.
            max_tokens=min(32768, max(8000, 2200 * min(len(segments), 12))) if compact else 8000,
        )
        stage = "parse"
        source = _json_object(raw)
        parsed = _normalize_plan(source, len(segments))
        stage = "validation"
        if target_duration and not outline:
            if sum(shot.get("duration", 0) for shot in parsed["shots"].values()) != target_duration:
                raise ValueError("invalid_duration_budget")
        if compact and set(parsed["shots"]) != {str(i) for i in range(len(segments))}:
            raise ValueError("incomplete_shots")
        if compact:
            validation_category = "invalid_shot_structure"
            for shot in source.get("shots") or []:
                motion = str(shot.get("motion_prompt") or "").strip()
                parts = re.split("[;；]", motion)
                if (not 150 <= len(motion) <= 1800
                        or len(parts) < 4 or not re.search(r"\[[A-Za-z][^\]]*\]", motion)
                        or any(not shot.get(key) for key in ("camera", "start_state", "end_state"))):
                    raise ValueError("invalid_shot_structure")
    except Exception as failure:
        # Preserve original reliable generation path on model/JSON failure.
        from src.api.error_model import classify_exception
        category = (classify_exception(failure).code.value if stage == "provider"
                    else "invalid_json" if stage == "parse" else validation_category)
        error = {"stage": stage, "category": category}
        log.warning("[%s] 分镜导演规划失败 stage=%s category=%s", task_id, stage, category)
        parsed = {"visual_bible": {}, "shots": {}}
    if ip_bible:
        # Series identity always overrides per-episode model improvisation.
        parsed["visual_bible"] = {
            **parsed["visual_bible"], **{k: v for k, v in ip_bible.items() if v}
        }
    record = {
        "version": _PLAN_VERSION, "fingerprint": fingerprint,
        "status": "failed" if error else "completed", "error": error,
        "ip_id": (ip_profile or {}).get("ip_id"),
        "segments": list(segments), "outline": outline, "visual_bible": parsed["visual_bible"],
        "shots": parsed["shots"],
    }
    if outline and not error:
        for i, item in enumerate(outline):
            record["shots"][str(i)].update(duration=item["duration"], purpose=item["purpose"])
    record["continuity_warnings"] = audit_plan(record)
    _write(task_id, record)
    return record


def direct_segments(generator, script, *, target_duration=None):
    """Let the existing director assign narration boundaries before image planning.

    Only whitespace may change: no model-authored omissions or new dialogue can
    silently enter the approved script. A failed proposal is never substituted
    with short subtitle chunks.
    """
    import math
    target = int(target_duration or max(4, math.ceil(len(re.sub(r'\s+', '', script)) / 4)))
    if target > 180:
        raise ValueError("当前原生视频流程单次最多规划 180 秒，请缩短文稿")
    prompt = (
        "你是科普动画导演。请根据完整文稿决定分镜边界与逐镜时长，而不是按固定字数切句。"
        "每镜表达一个清晰的信息或动作，可用人物中景、食材特写、俯拍示范、无文字示意图；"
        "避免重复构图。不要把每一句话独立成镜，完整解释可以跨几句话。"
        "返回纯JSON：{\"shots\":[{\"text\":\"原文连续片段\",\"duration\":10,\"purpose\":\"本镜讲述目的和画面意图\"}]}。"
        "所有text按顺序拼接必须等于原文（仅允许空白变化），不得改写、删减、添加台词。"
        "duration是4到15秒整数，适合自然语速和动作；一般每秒3至5个中文字，留出停顿。"
        "最多12镜，所有duration之和必须等于目标秒数。根据内容选不同镜头时长，不必等长。"
        f"目标时长：{target}秒。完整原文：\n{script}"
    )
    last_error = None
    for attempt in range(2):
        messages = [{"role": "user", "content": prompt}]
        if last_error:
            messages.append({"role": "user", "content": "上一方案未通过校验：" + last_error + "。请重新完整设计。"})
        try:
            raw = _json_object(generator._call_api(messages=messages, max_tokens=16000))
            shots = raw.get("shots")
            if not isinstance(shots, list) or not 1 <= len(shots) <= 12:
                raise ValueError("镜头数必须是1至12")
            result = []
            for item in shots:
                duration = item.get("duration")
                if type(duration) is not int or not 4 <= duration <= 15:
                    raise ValueError("每镜时长必须是4至15秒整数")
                if not isinstance(item.get("text"), str) or not item["text"].strip() or not item.get("purpose"):
                    raise ValueError("每镜必须有完整原文和画面意图")
                result.append({"text": item["text"].strip(), "duration": duration, "purpose": str(item["purpose"])[:400]})
            clean = lambda value: re.sub(r"\s+", "", value)
            if clean("".join(item["text"] for item in result)) != clean(script):
                raise ValueError("分镜文本没有完整保留原文与顺序")
            if sum(item["duration"] for item in result) != target:
                raise ValueError("总时长不等于目标时长")
            return result
        except (ValueError, TypeError, KeyError, AttributeError) as failure:
            last_error = str(failure) if isinstance(failure, ValueError) else "分镜结构错误"
    raise ValueError("导演分镜边界规划失败，请重新生成预案")


def save_director_outline(task_id, outline):
    _write(task_id, {"version": _PLAN_VERSION, "status": "planning",
                     "segments": [item["text"] for item in outline], "outline": outline, "shots": {}})
