"""Local project-level H3 reference settings; never uploads to MiniMax here."""
import asyncio

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel
from typing import Literal

from src.database import mysql_client
from src.media import h3_references, shot_video_jobs, video_first
from src.media.minimax_i2v import is_h3_model
from src.text.storyboard_director import load_plan

router = APIRouter(prefix="/ai/native/video/kepu", tags=["h3-references"])


class ReferenceMode(BaseModel):
    mode: Literal["first_frame", "reference"]


def _context(task_id, *, editing=False):
    task = mysql_client.get_task(task_id)
    if not task:
        raise HTTPException(404, "项目不存在")
    segments = mysql_client.get_segments(task_id)
    plan = load_plan(task_id) or {}
    shots = plan.get("shots") or {}
    scene_ids = list(dict.fromkeys(
        str(shot.get("scene_id") or "")
        for shot in shots.values() if shot.get("scene_id")
    ))
    if editing:
        status = video_first.status(task_id, segments, ratio=task.get("ratio") or "16:9")
        if status.get("status") in {"generating", "rendering"}:
            raise HTTPException(409, "正在生成整片，暂不能修改参考素材")
        if any(
            shot_video_jobs.status(task_id, int(seg.get("segment_index") or 0)).get("status") == "running"
            for seg in segments
        ):
            raise HTTPException(409, "正在生成单镜头，暂不能修改参考素材")
    return scene_ids


def _view(task_id, scene_ids):
    result = h3_references.overview(task_id, scene_ids)
    config = shot_video_jobs._video_config()
    result["supports_reference"] = (
        shot_video_jobs.video_provider() == "minimax"
        and is_h3_model(config.get("model"))
    )
    result["model"] = (config.get("model") or "") if result["supports_reference"] else None
    return result


@router.get("/tasks/{task_id}/h3-references")
async def get_reference_settings(task_id: str):
    scene_ids = await asyncio.to_thread(_context, task_id)
    return _view(task_id, scene_ids)


@router.put("/tasks/{task_id}/h3-references")
async def set_reference_mode(task_id: str, payload: ReferenceMode):
    scene_ids = await asyncio.to_thread(_context, task_id, editing=True)
    if payload.mode == "reference" and not _view(task_id, scene_ids)["supports_reference"]:
        raise HTTPException(409, "请先在视频配置选择 MiniMax-H3 或 MiniMax-H3-Max")
    await asyncio.to_thread(h3_references.select_mode, task_id, payload.mode)
    return _view(task_id, scene_ids)


@router.post("/tasks/{task_id}/h3-references/scenes/{scene_id}")
async def upload_scene_reference(task_id: str, scene_id: str, file: UploadFile = File(...)):
    scene_ids = await asyncio.to_thread(_context, task_id, editing=True)
    if scene_id not in scene_ids:
        raise HTTPException(404, "该场景不在当前分镜规划中")
    # Boundary the actual read, regardless of client-provided content-length.
    raw = await file.read(12 * 1024 * 1024 + 1)
    try:
        await asyncio.to_thread(h3_references.save_scene_image, task_id, scene_id, raw)
    except h3_references.H3ReferenceError as error:
        raise HTTPException(400, str(error)) from None
    return _view(task_id, scene_ids)


@router.delete("/tasks/{task_id}/h3-references/scenes/{scene_id}")
async def remove_scene_reference(task_id: str, scene_id: str):
    scene_ids = await asyncio.to_thread(_context, task_id, editing=True)
    if scene_id not in scene_ids:
        raise HTTPException(404, "该场景不在当前分镜规划中")
    await asyncio.to_thread(h3_references.clear_scene, task_id, scene_id)
    return _view(task_id, scene_ids)


@router.post("/tasks/{task_id}/h3-references/voice")
async def upload_voice_reference(task_id: str, file: UploadFile = File(...)):
    scene_ids = await asyncio.to_thread(_context, task_id, editing=True)
    raw = await file.read(8 * 1024 * 1024 + 1)
    try:
        await asyncio.to_thread(h3_references.save_voice_sample, task_id, raw)
    except h3_references.H3ReferenceError as error:
        raise HTTPException(400, str(error)) from None
    return _view(task_id, scene_ids)


@router.delete("/tasks/{task_id}/h3-references/voice")
async def remove_voice_reference(task_id: str):
    scene_ids = await asyncio.to_thread(_context, task_id, editing=True)
    await asyncio.to_thread(h3_references.clear_voice, task_id)
    return _view(task_id, scene_ids)
