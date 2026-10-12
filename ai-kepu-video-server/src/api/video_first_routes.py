"""Strict image-to-video production endpoints, deliberately separate from TTS.

Each shot's AI-generated MP4 is mandatory. A missing clip blocks the final
MP4; the static image exporter is NEVER used by these endpoints.
"""
import asyncio
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.database import mysql_client
from src.media import shot_video_jobs, video_first

router = APIRouter(prefix="/ai/native/video/kepu", tags=["video-first"])


class StartVideoFirst(BaseModel):
    confirm_paid_generation: bool = Field(default=False)
    duration: int | None = Field(default=None)
    force_new_failed: bool = Field(default=False)
    audio_mode: Optional[Literal["native", "mute"]] = None
    subtitles: bool | None = None
    max_duration: float | None = Field(default=None, gt=0, le=3600)


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
    value["video_provider"] = shot_video_jobs.video_provider()
    value["duration_options"] = list(shot_video_jobs.duration_options())
    value["default_duration"] = shot_video_jobs.default_duration()
    if value.get("status") == "not_started":
        from src.media.minimax_i2v import is_h3_model
        value["subtitles"] = (shot_video_jobs.video_provider() == "minimax"
                              and is_h3_model(shot_video_jobs._video_config().get("model")))
        value["max_duration"] = len(segments)*value["default_duration"] if value["subtitles"] else None
    return value


class AudioPreference(BaseModel):
    audio_mode: Literal["native", "mute"]


@router.put("/tasks/{task_id}/video-first/audio-preference")
async def update_video_audio_preference(task_id: str, payload: AudioPreference):
    await asyncio.to_thread(_project, task_id)
    mode = await asyncio.to_thread(
        video_first.set_audio_preference, task_id, payload.audio_mode
    )
    return {"preferred_audio_mode": mode}


@router.get("/tasks/{task_id}/video-first")
async def get_video_first(task_id: str, request: Request):
    task, segments = await asyncio.to_thread(_project, task_id)
    return _view(task_id, task, segments, request)


@router.post("/tasks/{task_id}/video-first", status_code=202)
async def start_video_first(task_id: str, data: StartVideoFirst, request: Request):
    task, segments = await asyncio.to_thread(_project, task_id)
    if not data.confirm_paid_generation:
        raise HTTPException(400, "必须明确确认图生视频模型的付费调用")
    duration = shot_video_jobs.default_duration() if data.duration is None else data.duration
    options = shot_video_jobs.duration_options()
    if duration not in options:
        raise HTTPException(
            400,
            "单分镜视频生成仅支持 " + "、".join(str(value) for value in options) + " 秒",
        )
    try:
        await asyncio.to_thread(
            video_first.start, task_id, segments,
            ratio=task.get("ratio") or "16:9",
            duration=duration,
            confirmed=True,
            force_new_failed=data.force_new_failed,
            audio_mode=data.audio_mode,
            subtitles=data.subtitles,
            max_duration=data.max_duration,
        )
    except video_first.VideoFirstError as error:
        raise HTTPException(409, str(error)) from None
    return _view(task_id, task, segments, request)


class LocalDelivery(BaseModel):
    subtitles: bool = True
    max_duration: float | None = Field(default=None, gt=0, le=3600)


@router.post("/tasks/{task_id}/video-first/delivery", status_code=202)
async def retry_local_delivery(task_id: str, data: LocalDelivery, request: Request):
    task, segments = await asyncio.to_thread(_project, task_id)
    try:
        await asyncio.to_thread(
            video_first.retry_delivery, task_id, segments, ratio=task.get("ratio") or "16:9",
            subtitles=data.subtitles, max_duration=data.max_duration,
        )
    except video_first.VideoFirstError as error:
        raise HTTPException(409, str(error)) from None
    return _view(task_id, task, segments, request)


@router.post("/tasks/{task_id}/video-first/cancel")
async def cancel_video_first(task_id: str):
    await asyncio.to_thread(_project, task_id)
    requested = await asyncio.to_thread(video_first.request_stop, task_id)
    return {
        "outcome": "stop_requested" if requested else "not_running",
        "message": (
            "后续图生视频付费任务已要求停止；已提交的任务可能继续产生费用"
            if requested else "没有正在运行的图生视频任务"
        ),
    }


@router.get("/tasks/{task_id}/video-first/file", name="download_video_first")
async def download_video_first(task_id: str):
    task, segments = await asyncio.to_thread(_project, task_id)
    path = video_first.completed_path(task_id, segments, ratio=task.get("ratio") or "16:9")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "动态视频尚未全部完成或已过期")
    return FileResponse(path, filename="video_first.mp4", media_type="video/mp4")
