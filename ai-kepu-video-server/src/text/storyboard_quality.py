"""Checkpointed, bounded review of a candidate; never dispatches media."""
from copy import deepcopy

from src.api.error_model import classify_exception
from src.api.task_runtime import TaskCancelled
from src.text.storyboard_review import repair_shots, review_plan

_LOCAL_ERRORS = {
    "invalid_shot_structure": "分镜格式不合规，已保留原候选。",
    "incomplete_shots": "分镜数量或编号不完整，已保留原候选。",
    "invalid_duration_budget": "分镜总时长不符合已确认的预算，已保留原候选。",
    "invalid_local_patch": "局部修复镜头范围不合规，已保留原候选。",
    "changed_shot_duration": "局部修复改变了已确认的镜头时长，已保留原候选。",
    "local_patch_changed_neighbor": "局部修复影响了未要求修改的镜头，已保留原候选。",
    "semantic_review_missing_shots": "审查缺少完整镜头，已保留原候选。",
    "semantic_review_invalid_index": "审查镜头编号不合规，已保留原候选。",
    "semantic_review_invalid_schema": "审查返回格式或版本不合规，已保留原候选。",
    "semantic_review_invalid_issue": "审查问题分类或严重程度不合规，已保留原候选。",
    "semantic_review_inconsistent_status": "审查结论与问题严重程度矛盾，已保留原候选。",
    "semantic_review_invalid_json": "审查未返回完整有效的 JSON，已保留原候选。",
    "semantic_review_global_or_missing_targets": "审查包含全局问题或缺少局部修复目标，已保留原候选。",
    "semantic_repair_invalid_schema": "局部修复返回格式不合规，已保留原候选。",
    "semantic_repair_wrong_targets": "局部修复镜头范围与审查要求不一致，已保留原候选。",
    "semantic_repair_changed_narration_or_duration": "局部修复改变了原文或已确认的时长，已保留原候选。",
    "分镜图片描述为空": "分镜图片描述为空，已保留原候选。",
    "导演画面契约格式无效": "分镜画面契约格式不合规，已保留原候选。",
    "分镜主体、景别或文字策略不受支持": "分镜主体、景别或文字策略不合规，已保留原候选。",
    "完整分镜约束超过服务商提示词长度上限，请由导演精简；不得截断规则": "完整画面约束超出提示词上限，需要精简，已保留原候选。",
}
_COMPILE_CODES = {
    "分镜图片描述为空": "image_prompt_empty",
    "导演画面契约格式无效": "visual_contract_invalid",
    "分镜主体、景别或文字策略不受支持": "visual_contract_unsupported",
    "完整分镜约束超过服务商提示词长度上限，请由导演精简；不得截断规则": "image_prompt_over_limit",
}


def _safe_failure(error, stage):
    # Match only exact fixed ValueError arguments. Never persist arbitrary text.
    if type(error) is ValueError and len(error.args) == 1 and isinstance(error.args[0], str):
        known = error.args[0]
        if known in _LOCAL_ERRORS:
            code = _COMPILE_CODES.get(known, known)
            message = _LOCAL_ERRORS[known]
            if stage == "repair" and known == "invalid_shot_structure":
                message = "修复格式不合规，已保留原候选；请重新审查并修正。"
            return {"stage": stage, "code": code, "category": code,
                    "safe_message": message, "retryable": False}
    safe = classify_exception(error)
    return {"stage": stage, "code": safe.code.value, "category": safe.code.value,
            "safe_message": safe.safe_message, "http_status": safe.http_status, "retryable": False}


def run_quality_loop(task_id, record, generator, *, script, style, ratio, ip_profile=None, cancel_check=None):
    from src.text.storyboard_director import (
        _QUALITY_VERSION, _candidate_digest, _director_output_budget, _normalize_plan,
        _validate_source, _write,
    )
    quality = {"version": _QUALITY_VERSION, "status": "pending", "issues": [],
               "review_rounds": 0, "repair_rounds": 0}
    record.update(review_required=True, quality=quality)
    record.setdefault("review_history", [])
    record.setdefault("repair_history", [])
    context = dict(script=script, style=style, ratio=ratio, ip_profile=ip_profile,
                   max_tokens=_director_output_budget(generator, 12000), cancel_check=cancel_check)
    stage = "review"
    try:
        for round_index in range(2):
            record.update(status="reviewing", error=None)
            _write(task_id, record)
            if cancel_check:
                cancel_check()
            review = review_plan(generator, record, **context)
            quality.update(status=review["status"], issues=review["issues"],
                           review_rounds=quality["review_rounds"] + 1)
            record["review_history"].append({"candidate_digest": _candidate_digest(record), "review": deepcopy(review)})
            if review["status"] == "passed":
                quality["candidate_digest"] = _candidate_digest(record)
                record.update(status="completed", error=None)
                _write(task_id, record)
                return record
            record.update(status="failed", error={"stage": "review", "category": "semantic_rejected"})
            _write(task_id, record)
            blockers = [issue for issue in review["issues"] if issue["severity"] == "blocking"]
            if round_index or any(issue["index"] is None for issue in blockers):
                return record
            stage = "repair"
            record["status"] = "repairing"
            _write(task_id, record)
            if cancel_check:
                cancel_check()
            patch = repair_shots(generator, record, review, **context)
            targets = {issue["index"] for issue in blockers}
            replacements = patch.get("shots")
            if (not isinstance(replacements, list)
                    or len(replacements) != len(targets)
                    or {s.get("index") for s in replacements} != targets):
                raise ValueError("invalid_local_patch")
            merged = deepcopy(record)
            for replacement in replacements:
                index = str(replacement["index"])
                if replacement.get("duration") != record["shots"][index].get("duration"):
                    raise ValueError("changed_shot_duration")
                merged["shots"][index] = replacement
            source = {"visual_bible": merged["visual_bible"],
                      "shots": [dict(s, index=int(k)) for k, s in merged["shots"].items()]}
            _validate_source(source, len(record["segments"]), reviewed=True,
                             outline=record.get("outline"))
            normalized = _normalize_plan(source, len(record["segments"]))
            # Normalize the whole topology, but do not silently change a neighbor
            # (e.g. a preceding continue transition when a repaired scene changes).
            for key, old in record["shots"].items():
                new = normalized["shots"][key]
                if "narrative_purpose" in old:
                    new["narrative_purpose"] = old["narrative_purpose"]
                if int(key) not in targets and new != old:
                    raise ValueError("local_patch_changed_neighbor")
            before = {str(i): deepcopy(record["shots"][str(i)]) for i in targets}
            record["shots"] = normalized["shots"]
            record["repair_history"].append({"indexes": sorted(targets), "before": before,
                "after": {str(i): deepcopy(record["shots"][str(i)]) for i in targets}})
            quality["repair_rounds"] += 1
            quality["status"] = "pending"
            _write(task_id, record)
            stage = "review"
    except Exception as error:
        quality.update(status="failed")
        record.update(status="failed", error=_safe_failure(error, stage))
        _write(task_id, record)
        if isinstance(error, TaskCancelled):
            raise
    return record
