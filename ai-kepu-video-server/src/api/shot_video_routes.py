"""Optional paid AI video generation for one saved storyboard image."""
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.database import mysql_client
from src.media import shot_video_jobs

router = APIRouter(prefix="/ai/native/video/kepu", tags=["shot-video"])


class CreateShotVideo(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=1500)
    duration: int = Field(default=5, ge=2, le=15)
    force_new: bool = False


def _segment(task_id: str, index: int) -> dict:
    if not mysql_client.get_task(task_id):
        raise HTTPException(404, "项目不存在")
    segment = next(
        (s for s in mysql_client.get_segments(task_id) if int(s["segment_index"]) == index),
        None,
    )
    if segment is None:
        raise HTTPException(404, "分镜不存在")
    if not segment.get("image_path") or not Path(segment["image_path"]).is_file():
        raise HTTPException(409, "请先生成本分镜首帧图片")
    return segment


def _view(task_id: str, index: int, segment: dict, request: Request) -> dict:
    item = shot_video_jobs.status(task_id, index, segment.get("image_path"))
    state = item.get("status", "not_generated")
    # Never expose internal provider URLs or filesystem paths to the browser.
    return {
        "status": state,
        "duration": item.get("duration"),
        "prompt": item.get("prompt"),
        "video_url": str(request.url_for("get_shot_video_file", task_id=task_id, segment_index=index))
                     if state == "completed" else None,
        "has_saved_task": bool(item.get("provider_task_id")),
        "error": item.get("error") if state in {"failed", "interrupted"} else None,
    }


@router.get("/tasks/{task_id}/segments/{segment_index}/video")
async def get_shot_video_status(task_id: str, segment_index: int, request: Request):
    segment = await asyncio.to_thread(_segment, task_id, segment_index)
    return _view(task_id, segment_index, segment, request)


@router.post("/tasks/{task_id}/segments/{segment_index}/video", status_code=202)
async def generate_shot_video(
    task_id: str, segment_index: int, data: CreateShotVideo, request: Request,
):
    segment = await asyncio.to_thread(_segment, task_id, segment_index)
    # Explicit action starts a paid call. No automatic dispatch from project confirmation.
    if data.force_new and shot_video_jobs.status(task_id, segment_index).get("status") == "running":
        raise HTTPException(409, "当前分镜已有生成任务")
    try:
        await asyncio.to_thread(
            shot_video_jobs.start, task_id, segment_index,
            segment["image_path"], data.prompt,
            duration=data.duration, force_new=data.force_new,
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "无法启动万相任务，请检查百炼 API Key 和模型配置") from exc
    return _view(task_id, segment_index, segment, request)


@router.get("/tasks/{task_id}/segments/{segment_index}/video/file", name="get_shot_video_file")
async def get_shot_video_file(task_id: str, segment_index: int):
    segment = await asyncio.to_thread(_segment, task_id, segment_index)
    path = shot_video_jobs.matching_video_path(task_id, segment)
    if not path:
        raise HTTPException(404, "可用的视频片段不存在")
    return FileResponse(path, media_type="video/mp4")
