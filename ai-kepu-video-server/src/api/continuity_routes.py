"""Read-only storyboard continuity inspection; no provider requests or charges."""
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException

from src.database import mysql_client
from src.media import h3_references, shot_video_jobs
from src.text.native_dialogue import compile_video_prompt
from src.text.continuity import audit_plan, compose_motion_prompt
from src.text.ip_library import project_ip
from src.text.storyboard_director import load_plan, lookup_shot


router = APIRouter(prefix="/ai/native/video/kepu", tags=["continuity"])


def _report(task_id):
    task = mysql_client.get_task(task_id)
    if not task:
        raise HTTPException(404, "项目不存在")
    segments = sorted(
        mysql_client.get_segments(task_id),
        key=lambda seg: int(seg.get("segment_index") or 0),
    )
    plan = load_plan(task_id)
    if not plan:
        return {
            "status": "unplanned", "shots": [], "warnings": [{
                "index": None, "code": "plan_missing",
                "message": "本项目尚无新版一致性分镜计划；请重新生成预案",
            }],
            "ip": None, "visual_bible": {}, "actual_frame_bridge": False,
            "audio_note": "H3 原生讲解可引用 IP 或项目声音样本；没有参考时仅使用统一声线描述",
        }
    profile = project_ip(task_id)
    shots = []
    prompt_warnings = []
    voice = h3_references.overview(task_id)
    refs = {"audio": True} if voice["voice_ready"] and voice["mode"] == "reference" else None
    for segment in segments:
        index = int(segment.get("segment_index") or 0)
        shot = lookup_shot(task_id, index, segment.get("text") or "")
        if not shot:
            continue
        prompt = compose_motion_prompt(plan, index, shot.get("motion_prompt") or "")
        try:
            prompt = compile_video_prompt(task_id, segment.get("text"), prompt, refs, shot_video_jobs._video_config())
        except ValueError as error:
            prompt_warnings.append({"index": index, "code": "dialogue_invalid", "message": str(error)})
        shot_view = {
            "index": index,
            "scene_id": shot.get("scene_id") or "",
            "character_ids": shot.get("character_ids") or [],
            "location_id": shot.get("location_id") or "",
            "camera": shot.get("camera") or "",
            "start_state": shot.get("start_state") or "",
            "end_state": shot.get("end_state") or "",
            "transition_to_next": shot.get("transition_to_next") or "cut",
            "image_ready": bool(segment.get("image_path") and
                                Path(segment["image_path"]).is_file()),
            "model_prompt": prompt,
        }
        shots.append(shot_view)
    warnings = [*audit_plan(plan), *prompt_warnings]
    if len(shots) < len(segments):
        warnings = [*warnings, {
            "index": None, "code": "unplanned_shots",
            "message": "部分镜头缺少新版导演规划，不能保证同场景状态一致",
        }]
    return {
        "status": "planned" if len(shots) == len(segments) else "partial",
        "visual_bible": plan.get("visual_bible") or {},
        "ip": ({"name": profile.get("name"), "version": profile.get("version")}
               if profile else None),
        "shots": shots,
        "warnings": warnings,
        "actual_frame_bridge": False,
        "audio_note": (
            "H3 原生对白按分镜原文生成，可使用同一 IP 或项目声音参考；"
            "参考有助于音色一致，仍需检查实际成片。"
        ),
    }


@router.get("/tasks/{task_id}/continuity")
async def get_task_continuity(task_id: str):
    return await asyncio.to_thread(_report, task_id)
