"""Strict image-to-video production endpoints, deliberately separate from TTS.

Each shot's AI-generated MP4 is mandatory. A missing clip blocks the final
MP4; the static image exporter is NEVER used by these endpoints.
"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.database import mysql_client
from src.media import video_first

router = APIRouter(prefix="/ai/native/video/kepu", tags=["video-first"])


class StartVideoFirst(BaseModel):
    confirm_paid_generation: bool = Field(default=False)
    duration: int = Field(default=5)
    force_new_failed: bool = Field(default=False)


def _project(task_id):
    task = mysql_client.get_task(task_id)
    if not task:
        raise HTTPException(404, "项目不存在")
    segments = mysql_client.get_segments(task_id)
    return task, segments


def _view(task_id, task, segments, request):
    value = video_first.status(task_id, segments, ratio=task.get("ratio") or "16:9")
    value["video_url"] = (
        str(request.url_for("download_video_first", task_id=task_id))
        if value.get("has_video") else None
    )
    value["requires_all_motion_clips"] = True
    value["narration_required"] = False
    return value


@router.get("/tasks/{task_id}/video-first")
async def get_video_first(task_id: str, request: Request):
    task, segments = await asyncio.to_thread(_project, task_id)
    return _view(task_id, task, segments, request)


@router.post("/tasks/{task_id}/video-first", status_code=202)
async def start_video_first(task_id: str, data: StartVideoFirst, request: Request):
    task, segments = await asyncio.to_thread(_project, task_id)
    if not data.confirm_paid_generation:
        raise HTTPException(400, "必须明确确认图生视频模型的付费调用")
    if data.duration not in (5, 10, 15):
        raise HTTPException(400, "单分镜视频生成仅支持 5、10 或 15 秒")
    try:
        await asyncio.to_thread(
            video_first.start, task_id, segments,
            ratio=task.get("ratio") or "16:9",
            duration=data.duration,
            confirmed=True,
            force_new_failed=data.force_new_failed,
        )
    except video_first.VideoFirstError as error:
        raise HTTPException(409, str(error)) from None
    return _view(task_id, task, segments, request)


@router.get("/tasks/{task_id}/video-first/file", name="download_video_first")
async def download_video_first(task_id: str):
    task, segments = await asyncio.to_thread(_project, task_id)
    path = video_first.completed_path(task_id, segments, ratio=task.get("ratio") or "16:9")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "动态视频尚未全部完成或已过期")
    return FileResponse(path, filename="video_first.mp4", media_type="video/mp4")
