"""Read-only saved inspection and explicitly confirmed background review."""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from src.api.task_runtime import task_runtime
from src.api.work_limits import generation_pool
from src.media import image_review

router = APIRouter(prefix='/ai/native/video/kepu', tags=['image-review'])


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_model_call: StrictBool = False
    model: str = 'qwen3-vl-plus'


def _worker(task_id, index, token, current, model):
    result = {k: current[k] for k in ('image_hash', 'contract_hash')}
    result.update(status='running', verdict='unknown', checks=[], reviewer_model=model,
                  reviewer_version=image_review.VERSION)
    try:
        token.raise_if_cancelled()
        image_review.save(task_id, index, result)
        verdict, checks = image_review.inspect(current, model)
        if current.get('receipt'):
            result['receipt'] = current['receipt']
        token.raise_if_cancelled()
        latest = image_review.snapshot(task_id, index)
        if any(latest[k] != current[k] for k in ('image_hash', 'contract_hash')):
            raise image_review.ReviewError('审查期间图片或要求已变化')
        result.update(status='completed', verdict=verdict, checks=checks)
    except Exception:
        result.update(status='failed', verdict='unknown', checks=[], error='视觉审查未完成，请检查配置或重新审查')
    finally:
        try:
            image_review.save(task_id, index, result)
        finally:
            task_runtime.finish(task_id, token)


def _start(task_id, index, body):
    if body.confirm_model_call is not True:
        raise HTTPException(400, '请明确确认本次视觉模型调用及 token 费用')
    try:
        image_review.validate_config(body.model)
        current = image_review.snapshot(task_id, index)
    except image_review.ReviewError as error:
        raise HTTPException(400, str(error)) from None
    token = task_runtime.begin(task_id)
    if token is None:
        raise HTTPException(409, '项目已有操作或正在关闭')
    result = {k: current[k] for k in ('image_hash', 'contract_hash')}
    result.update(status='pending', verdict='unknown', checks=[], reviewer_model=body.model,
                  reviewer_version=image_review.VERSION)
    try:
        image_review.save(task_id, index, result)
        future = generation_pool.submit(_worker, task_id, index, token, current, body.model)
        def cancelled():
            try:
                image_review.save(task_id, index, {**result, 'status': 'failed', 'error': '审查已取消'})
            finally:
                task_runtime.finish(task_id, token)
        token.bind_future(future, cancelled)
    except Exception:
        task_runtime.finish(task_id, token)
        raise HTTPException(500, '视觉审查无法启动') from None
    return result


@router.post('/tasks/{task_id}/segments/{index}/image-review', status_code=202)
async def start_image_review(task_id: str, index: int, body: ReviewRequest):
    return await asyncio.to_thread(_start, task_id, index, body)


@router.get('/tasks/{task_id}/segments/{index}/image-review')
async def get_image_review(task_id: str, index: int):
    try:
        return await asyncio.to_thread(image_review.read, task_id, index)
    except image_review.ReviewError as error:
        raise HTTPException(400, str(error)) from None
