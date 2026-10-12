"""Typed continuity contracts for AI-directed, independently generated shots.

Normal edits are cinematographic CUTS, not first/last-frame interpolation.
"continue" is a requested *semantic* action link. The current Wan/MiniMax
adapters still take one first-frame image per shot; a hard visual bridge needs
a supported video-to-video/first-last-frame provider and is not implemented.
"""
import re

TRANSITIONS = frozenset({"cut", "match_cut", "continue"})
MAX_PROMPT = 1800


def _short(value, size):
    return str(value or "").strip()[:size]


def normalize_shot(shot, index):
    """Drop malformed model fields; never let model output control file paths."""
    scene = _short(shot.get("scene_id"), 64)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", scene):
        scene = f"scene_{index:03d}"
    cast = shot.get("character_ids")
    if not isinstance(cast, list):
        cast = []
    cast = [
        _short(name, 50) for name in cast[:8]
        if isinstance(name, str) and _short(name, 50)
    ]
    transition = _short(shot.get("transition_to_next"), 30).lower()
    if transition not in TRANSITIONS:
        transition = "cut"
    return {
        "scene_id": scene,
        "character_ids": cast,
        "location_id": _short(shot.get("location_id"), 80),
        "camera": _short(shot.get("camera"), 120),
        "start_state": _short(shot.get("start_state"), 360),
        "end_state": _short(shot.get("end_state"), 360),
        "transition_to_next": transition,
    }


def normalize_boundaries(shots):
    """Prevent impossible continuous actions between clearly different sets."""
    ordered = sorted((int(key), value) for key, value in shots.items())
    for pos, (_, current) in enumerate(ordered):
        if pos == len(ordered) - 1:
            current["transition_to_next"] = "cut"
            continue
        next_shot = ordered[pos + 1][1]
        if (current.get("transition_to_next") == "continue"
                and current.get("scene_id") != next_shot.get("scene_id")):
            current["transition_to_next"] = "cut"
    return shots


def compose_motion_prompt(plan, index, fallback, limit=MAX_PROMPT):
    """Encode shot states and editorial semantics in the *actual* I2V prompt.

    A cut can move between camera angles or locations; it must not claim that
    the previous final frame is identical to the current first frame.
    The provider receives an independent first-frame image for this shot.
    """
    shots = (plan or {}).get("shots") or {}
    current = shots.get(str(index)) or {}
    if not current:
        return _short(fallback, limit)
    previous = shots.get(str(index - 1)) or {}
    following = shots.get(str(index + 1)) or {}
    incoming = previous.get("transition_to_next", "cut")
    if incoming not in TRANSITIONS:
        incoming = "cut"
    if incoming == "continue" and previous.get("scene_id") != current.get("scene_id"):
        incoming = "cut"
    if incoming == "continue":
        lead = (
            "连续动作镜头：承接前一镜头的动作、运动方向和人物站位，"
            "但本镜头仍以自己的首帧图为真实输入，不要假装跨片段像素无缝。"
        )
    elif incoming == "match_cut":
        lead = "匹配剪辑：延续前一镜头的视觉母题或动作方向，但允许新的机位和构图，不要求两帧像素相同。"
    else:
        lead = "正常切镜：本镜头是独立构图，不要复制上一段的尾帧；只保持已确定的角色、场景和画风设定。"
    bible = (plan or {}).get("visual_bible") or {}
    locks = [
        f"角色锁定：{_short(bible.get('character_lock'), 230)}" if bible.get("character_lock") else "",
        f"画风锁定：{_short(bible.get('art_direction'), 170)}" if bible.get("art_direction") else "",
        f"角色ID：{','.join(current.get('character_ids') or [])}" if current.get("character_ids") else "",
        f"场景：{_short(current.get('scene_id'), 64)}；地点：{_short(current.get('location_id'), 70)}",
        f"景别机位：{_short(current.get('camera'), 100)}" if current.get("camera") else "",
        f"开场状态：{_short(current.get('start_state'), 220)}" if current.get("start_state") else "",
        (f"上一镜头结束状态：{_short(previous.get('end_state'), 210)}"
         if previous and incoming in {"continue", "match_cut"} else ""),
        f"结束状态：{_short(current.get('end_state'), 220)}" if current.get("end_state") else "",
        (f"接往下一镜头的方式：{current.get('transition_to_next', 'cut')}；"
         f"下一镜头开场：{_short(following.get('start_state'), 170)}"
         if following else "收尾请保持稳定构图"),
    ]
    # Place constraints before motion prose to resist truncation. Preserve a
    # meaningful motion instruction even when the shot has a detailed bible.
    direction = "；".join(item for item in locks if item)
    direction = direction[:1100]
    narrative = _short(fallback or current.get("motion_prompt"), max(0, limit - 200))
    return f"{lead}；{direction}；动作与运镜：{narrative}"[:limit]


def audit_plan(plan):
    """Cheap deterministic preflight: warn, never claim an identity guarantee."""
    shots = (plan or {}).get("shots") or {}
    items = sorted(((int(k), v) for k, v in shots.items()), key=lambda x: x[0])
    warnings = []
    for position, (index, shot) in enumerate(items):
        if not shot.get("start_state") or not shot.get("end_state"):
            warnings.append({
                "index": index, "code": "state_missing",
                "message": "缺少开场/收尾状态，建议人工审核镜头衔接",
            })
        if position == len(items) - 1:
            continue
        following_index, next_shot = items[position + 1]
        if following_index != index + 1:
            warnings.append({
                "index": index, "code": "shot_gap",
                "message": "缺少相邻分镜的导演规划，无法检查镜头衔接",
            })
        elif shot.get("transition_to_next") == "continue":
            if shot.get("scene_id") != next_shot.get("scene_id"):
                warnings.append({
                    "index": index, "code": "scene_mismatch",
                    "message": "连续动作跨越不同场景，建议改为切镜",
                })
            elif not shot.get("end_state") or not next_shot.get("start_state"):
                warnings.append({
                    "index": index, "code": "missing_action_anchor",
                    "message": "连续动作缺少前镜头结尾与后镜头开场锚点",
                })
    return warnings
