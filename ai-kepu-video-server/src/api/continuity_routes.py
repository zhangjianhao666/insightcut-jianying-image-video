"""Storyboard review and explicit background replanning; never submits paid video."""
import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from src.database import mysql_client
from src.media import h3_references, shot_video_jobs, video_first
from src.api.task_runtime import task_runtime
from src.api.work_limits import generation_pool
from src.api.error_model import classify_exception
from src.text.generator import ArticleGenerator
from src.text.storyboard_director import plan_storyboard, record_planning_failure
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
    operation = mysql_client.get_active_task_operation(task_id)
    if operation and operation.get("kind") == "director_replan":
        return {
            "status": "planning", "error": None, "planning_error": None,
            "operation": {"operation_id": operation.get("operation_id"),
                          "status": operation.get("state")},
            "can_replan": False, "shots": [], "warnings": [],
            "visual_bible": {}, "ip": None, "actual_frame_bridge": False,
            "audio_note": "正在重新规划，已有素材仍保留",
        }
    plan = load_plan(task_id)
    if not plan:
        return {
            "status": "unplanned", "planning_error": None, "can_replan": bool(segments),
            "shots": [], "warnings": [{
                "index": None, "code": "plan_missing",
                "message": "本项目尚无新版一致性分镜计划；请重新生成预案",
            }],
            "ip": None, "visual_bible": {}, "actual_frame_bridge": False,
            "audio_note": "H3 原生讲解可引用 IP 或项目声音样本；没有参考时仅使用统一声线描述",
        }
    if plan.get("status") == "failed":
        return {
            "status": "failed", "planning_error": plan.get("error"),
            "error": "导演规划未完成，请重新规划后再生成原生视频",
            "can_replan": bool(segments), "shots": [],
            "warnings": [{"index": None, "code": "director_failed",
                          "message": "导演规划未完成，请重新规划后再生成原生视频"}],
            "ip": None, "visual_bible": plan.get("visual_bible") or {},
            "actual_frame_bridge": False,
            "audio_note": "已有图片与视频素材仍保留",
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
            "duration": shot.get("duration"),
            "purpose": shot.get("purpose") or "",
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
        "planning_error": None, "can_replan": bool(segments),
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


def _replan_worker(task_id, operation_id, token, task, segments):
    try:
        token.raise_if_cancelled()
        mysql_client.update_task_operation(operation_id, state="running")
        result = plan_storyboard(
            task_id, ArticleGenerator(), force=True,
            script=task.get("script_text") or "\n".join(segments),
            segments=segments, summary=task.get("summary") or "",
            style=task.get("style") or "", ratio=task.get("ratio") or "16:9",
        )
        failed = result.get("status") == "failed"
        mysql_client.update_task_operation(
            operation_id, state="failed" if failed else "completed",
            completed_count=0 if failed else 1, failed_count=1 if failed else 0,
            error="导演规划未完成，请检查配置后重新规划" if failed else "",
            error_code="provider_error" if failed else None,
            error_meta={"stage": (result.get("error") or {}).get("stage", "validation")},
        )
    except Exception as failure:
        safe = classify_exception(failure)
        record_planning_failure(task_id, stage="planning", category=safe.code.value, segments=segments)
        mysql_client.update_task_operation(
            operation_id, state="failed", failed_count=1,
            error=safe.safe_message, error_code=safe.code.value,
            error_meta={"stage": "planning"},
        )
    finally:
        task_runtime.finish(task_id, token)


def _start_replan(task_id):
    task = mysql_client.get_task(task_id)
    if not task:
        raise HTTPException(404, "项目不存在")
    segments = sorted(mysql_client.get_segments(task_id), key=lambda s: int(s.get("segment_index") or 0))
    if not segments:
        raise HTTPException(409, "请先完成文稿分镜")
    state = video_first.status(task_id, segments, ratio=task.get("ratio") or "16:9").get("status")
    if state in {"generating", "rendering", "transcribing", "subtitling", "processing"} or mysql_client.production_list(task_id=task_id, active=True):
        raise HTTPException(409, "正在生产视频，暂不能重新规划")
    if any(shot_video_jobs.status(task_id, int(s.get("segment_index") or 0)).get("status") == "running" for s in segments):
        raise HTTPException(409, "正在生成镜头，暂不能重新规划")
    token = task_runtime.begin(task_id)
    if token is None:
        raise HTTPException(409, "项目已有操作或正在关闭，请稍后重试")
    operation_id = None
    try:
        key = uuid.uuid4().hex
        created = mysql_client.create_task_operation(
            task_id=task_id, kind="director_replan", idempotency_key="director:" + key,
            snapshot_key=key, targets=[],
        )
        if created.get("outcome") != "created":
            raise HTTPException(409, "项目已有操作，请稍后重新规划")
        operation = created["operation"]
        operation_id = operation["operation_id"]
        future = generation_pool.submit(_replan_worker, task_id, operation_id, token, task, [s.get("text") or "" for s in segments])
        def cancelled():
            mysql_client.update_task_operation(operation_id, state="cancelled")
            task_runtime.finish(task_id, token)
        token.bind_future(future, cancelled)
        return {"status": "planning", "operation": {"operation_id": operation_id, "status": operation.get("state", "pending")}}
    except Exception:
        if operation_id:
            mysql_client.update_task_operation(operation_id, state="failed", error="导演规划启动失败", error_code="provider_error")
        task_runtime.finish(task_id, token)
        raise


@router.post("/tasks/{task_id}/director/replan", status_code=202)
async def replan_director(task_id: str):
    return await asyncio.to_thread(_start_replan, task_id)
