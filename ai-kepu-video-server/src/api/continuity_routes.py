"""Storyboard review and explicit background replanning; never submits paid video."""
import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.api.task_runtime import TaskCancelled
from src.media.minimax_i2v import is_h3_model

from src.database import mysql_client
from src.media import h3_references, shot_video_jobs, video_first
from src.api.task_runtime import task_runtime
from src.api.work_limits import generation_pool
from src.api.error_model import classify_exception, sanitize_text
from src.text.generator import ArticleGenerator
from src.text.storyboard_director import plan_storyboard, record_planning_failure, planning_blocked, review_existing_plan
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
    plan = load_plan(task_id)
    if not plan:
        return {
            "status": "planning" if operation else "unplanned", "planning_error": None, "can_replan": bool(segments) and not operation,
            "shots": [], "warnings": [{
                "index": None, "code": "plan_missing",
                "message": "本项目尚无新版一致性分镜计划；请重新生成预案",
            }],
            "ip": None, "visual_bible": {}, "actual_frame_bridge": False,
            "audio_note": "H3 原生讲解可引用 IP 或项目声音样本；没有参考时仅使用统一声线描述",
        }
    interrupted = (plan.get("status") in {"planning", "reviewing", "repairing"}
                   and not operation and not task_runtime.is_running(task_id))
    plan_blocked = planning_blocked(plan)
    quality = dict(plan.get("quality") or {})
    if plan_blocked and plan.get("status") == "completed" and quality.get("status") == "passed":
        quality["status"] = "needs_review"
    blocked = plan_blocked or bool(operation and operation.get("kind") in {"director_replan", "director_review"})
    profile = project_ip(task_id)
    shots = []
    prompt_warnings = []
    voice = h3_references.overview(task_id)
    refs = {"audio": True} if voice["voice_ready"] and voice["mode"] == "reference" else None
    display_segments = list(segments)
    if blocked:
        indexes = {int(s.get("segment_index") or 0) for s in segments}
        display_segments.extend({"segment_index": int(key)} for key in (plan.get("shots") or {}) if str(key).isdigit() and int(key) not in indexes)
    for segment in display_segments:
        index = int(segment.get("segment_index") or 0)
        shot = (plan.get("shots") or {}).get(str(index), {}) if blocked else lookup_shot(task_id, index, segment.get("text") or "")
        if not shot:
            continue
        prompt = ""
        if not blocked:
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
            "image_prompt": shot.get("image_prompt") or "",
            "motion_prompt": shot.get("motion_prompt") or "",
            "narrative_purpose": shot.get("narrative_purpose") or "",
        }
        shots.append(shot_view)
    warnings = [*audit_plan(plan), *prompt_warnings]
    if len(shots) < len(segments):
        warnings = [*warnings, {
            "index": None, "code": "unplanned_shots",
            "message": "部分镜头缺少新版导演规划，不能保证同场景状态一致",
        }]
    return {
        "status": ("planning" if operation and operation.get("kind") == "director_replan" else ("repairing" if plan.get("status") == "repairing" else "reviewing") if operation and operation.get("kind") == "director_review" else "interrupted" if interrupted else plan.get("status")) if blocked else ("planned" if len(shots) == len(segments) else "partial"),
        "planning_error": plan.get("error"), "can_replan": bool(segments) and not operation,
        "can_review": bool(shots) and not operation,
        "generation_blocked": blocked, "quality": quality or None,
        "error": (sanitize_text((plan.get("error") or {}).get("safe_message")) or "导演规划尚未通过审查，已有素材仍保留") if blocked else None,
        "operation": {"operation_id": operation.get("operation_id"), "status": operation.get("state")} if operation else None,
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


def _replan_worker(task_id, operation_id, token, task, segments, review=False):
    try:
        token.raise_if_cancelled()
        mysql_client.update_task_operation(operation_id, state="running")
        style_parts = (task.get("style") or "").split("|", 2)
        visual_style = style_parts[1] if len(style_parts) > 1 and style_parts[1] else "写实风格"
        visual_style = style_parts[2] if len(style_parts) > 2 and style_parts[2] else visual_style
        context = dict(script=task.get("script_text") or "\n".join(segments),
                       style=None if review else visual_style, ratio=task.get("ratio") or "16:9",
                       cancel_check=token.raise_if_cancelled)
        if review:
            result = review_existing_plan(task_id, ArticleGenerator(), **context)
        else:
            result = plan_storyboard(
                task_id, ArticleGenerator(), force=True, segments=segments,
                summary=task.get("summary") or "",
                brief=str(task.get("original_input") or task.get("theme") or ""),
                review_required=shot_video_jobs.video_provider() == "minimax" and is_h3_model(shot_video_jobs._video_config().get("model")),
                **context,
            )
        token.raise_if_cancelled()
        failed = planning_blocked(result)
        if not failed:
            try:
                synchronized = mysql_client.sync_director_image_prompts(
                    task_id, {int(index): shot.get("image_prompt") or "" for index, shot in (result.get("shots") or {}).items()},
                    expected_plan_version=int(task.get("plan_version") or 0), expected_segments=segments,
                )
            except Exception:
                synchronized = None
            if synchronized is None or synchronized == -1:
                record_planning_failure(task_id, stage="synchronization", category="plan_conflict" if synchronized == -1 else "database_error", segments=segments)
                failed = True
                result = load_plan(task_id) or result
        mysql_client.update_task_operation(
            operation_id, state="failed" if failed else "completed",
            completed_count=0 if failed else 1, failed_count=1 if failed else 0,
            error="导演规划未完成，请检查配置后重新规划" if failed else "",
            error_code="provider_error" if failed else None,
            error_meta={"stage": (result.get("error") or {}).get("stage", "validation")},
        )
    except TaskCancelled:
        mysql_client.update_task_operation(operation_id, state="cancelled")
    except Exception as failure:
        safe = classify_exception(failure)
        if not review:
            record_planning_failure(task_id, stage="planning", category=safe.code.value, segments=segments)
        mysql_client.update_task_operation(
            operation_id, state="failed", failed_count=1,
            error=safe.safe_message, error_code=safe.code.value,
            error_meta={"stage": "planning"},
        )
    finally:
        task_runtime.finish(task_id, token)


def _start_replan(task_id, review=False):
    task = mysql_client.get_task(task_id)
    if not task:
        raise HTTPException(404, "项目不存在")
    segments = sorted(mysql_client.get_segments(task_id), key=lambda s: int(s.get("segment_index") or 0))
    if not segments:
        raise HTTPException(409, "请先完成文稿分镜")
    if review:
        plan = load_plan(task_id)
        if not plan or not plan.get("shots"):
            raise HTTPException(409, "没有可审查的分镜候选")
        if plan.get("segments") != [s.get("text") or "" for s in segments]:
            raise HTTPException(409, "文稿分镜已改变，请重新规划")
    state = video_first.status(task_id, segments, ratio=task.get("ratio") or "16:9").get("status")
    if state in {"generating", "rendering", "transcribing", "subtitling", "processing", "verifying", "cancelling"} or mysql_client.production_list(task_id=task_id, active=True):
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
            task_id=task_id, kind="director_review" if review else "director_replan", idempotency_key="director:" + key,
            snapshot_key=key, targets=[],
        )
        if created.get("outcome") != "created":
            raise HTTPException(409, "项目已有操作，请稍后重新规划")
        operation = created["operation"]
        operation_id = operation["operation_id"]
        future = generation_pool.submit(_replan_worker, task_id, operation_id, token, task, [s.get("text") or "" for s in segments], review)
        def cancelled():
            mysql_client.update_task_operation(operation_id, state="cancelled")
            task_runtime.finish(task_id, token)
        token.bind_future(future, cancelled)
        return {"status": "reviewing" if review else "planning", "operation": {"operation_id": operation_id, "status": operation.get("state", "pending")}}
    except Exception:
        if operation_id:
            mysql_client.update_task_operation(operation_id, state="failed", error="导演规划启动失败", error_code="provider_error")
        task_runtime.finish(task_id, token)
        raise


@router.post("/tasks/{task_id}/director/replan", status_code=202)
async def replan_director(task_id: str):
    return await asyncio.to_thread(_start_replan, task_id)


class DirectorReviewRequest(BaseModel):
    confirm_cost: bool = False


@router.post("/tasks/{task_id}/director/review", status_code=202)
async def review_director(task_id: str, body: DirectorReviewRequest):
    if not body.confirm_cost:
        raise HTTPException(400, "请明确确认审查与修正会产生模型 token 费用")
    return await asyncio.to_thread(_start_replan, task_id, True)
