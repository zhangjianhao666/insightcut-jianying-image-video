"""First-class AI video production: storyboard -> still -> I2V clip -> MP4.

No TTS, static image animation, voiceover duration, or legacy draft exporter
participates in this path. Every Wan job needs explicit batch approval.
Checkpoints survive browser reloads and service restarts. Existing paid job
IDs are queried rather than automatically resubmitted.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from src.config import Config
from src.media import h3_references, shot_video_jobs, native_delivery
from src.text.storyboard_director import DEFAULT_MOTION_PROMPT, load_plan, lookup_shot
from src.text.native_dialogue import compile_video_prompt
from src.text.continuity import compose_motion_prompt
from src.utils.rendering import normalize_ratio

MAX_SHOTS = 12
_ACTIVE = set()
_STOP = set()
_TOKENS = {}
_LOCK = threading.RLock()


class VideoFirstError(ValueError):
    pass


def _directory(task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(task_id)):
        raise VideoFirstError("非法项目 ID")
    return Config.BASE_DIR / "data" / "media" / "_video_first" / str(task_id)


def _record_path(task_id):
    return _directory(task_id) / "job.json"


def _audio_preference_path(task_id):
    return _directory(task_id) / "audio_preference.json"


def audio_preference(task_id):
    """Saved before paid generation; producer and standalone renderer agree."""
    try:
        data = json.loads(_audio_preference_path(task_id).read_text("utf-8"))
        mode = data.get("audio_mode")
        return mode if mode in {"native", "mute"} else "native"
    except (OSError, ValueError, AttributeError):
        return "native"


def set_audio_preference(task_id, mode):
    if mode not in {"native", "mute"}:
        raise VideoFirstError("音频策略不支持此选项")
    path = _audio_preference_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"audio_mode": mode}), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return mode


def _load(task_id):
    file = _record_path(task_id)
    if not file.is_file():
        return {"status": "not_started"}
    try:
        result = json.loads(file.read_text("utf-8"))
        return result if isinstance(result, dict) else {"status": "failed"}
    except (OSError, ValueError):
        return {"status": "failed", "error": "视频生产记录损坏"}


def _write(task_id, record):
    file = _record_path(task_id)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, file)
    finally:
        temporary.unlink(missing_ok=True)


def _shot_inputs(task_id, segments, ratio, duration):
    if not segments or len(segments) > MAX_SHOTS:
        raise VideoFirstError(f"视频优先模式支持 1–{MAX_SHOTS} 个分镜，建议先做短片验收")
    options = shot_video_jobs.duration_options()
    if duration not in options:
        raise VideoFirstError("单镜头时长只能是 " + "、".join(str(value) for value in options) + " 秒")
    result = []
    plan = load_plan(task_id) or {}
    if h3_references.mode(task_id) == "reference":
        from src.media.minimax_i2v import is_h3_model
        config = shot_video_jobs._video_config()
        if shot_video_jobs.video_provider() != "minimax" or not is_h3_model(config.get("model")):
            raise VideoFirstError("当前为 H3 参考生成模式，请先选择 MiniMax-H3 或 MiniMax-H3-Max")
    for segment in sorted(segments, key=lambda row: int(row.get("segment_index") or 0)):
        index = int(segment.get("segment_index") or 0)
        image = segment.get("image_path")
        if segment.get("image_status") != "completed" or not image or not Path(image).is_file():
            raise VideoFirstError(f"第 {index + 1} 个分镜的首帧图片尚未完成")
        direction = lookup_shot(task_id, index, segment.get("text") or "")
        motion_prompt = str(direction.get("motion_prompt") or "").strip()
        if not motion_prompt:
            motion_prompt = DEFAULT_MOTION_PROMPT
        if direction:
            motion_prompt = compose_motion_prompt(plan, index, motion_prompt)
        reference_assets = h3_references.for_shot(
            task_id, image, direction.get("scene_id") or "",
        )
        motion_prompt = compile_video_prompt(
            task_id, segment.get("text"), motion_prompt, reference_assets, shot_video_jobs._video_config(),
        )
        result.append({
            "reference_assets": reference_assets,
            "reference_fingerprint": (reference_assets or {}).get("fingerprint", "first_frame"),
            "index": index,
            "image": str(Path(image).resolve()),
            "signature": shot_video_jobs._identity(image),
            "text": str(segment.get("text") or ""),
            "motion_prompt": motion_prompt[:1800],
            "scene_id": direction.get("scene_id") or "",
            "transition_to_next": direction.get("transition_to_next") or "cut",
            "duration": duration,
        })
    return result


def _fingerprint(shots, ratio, audio_mode="native"):
    stable = [
        (item["index"], item["signature"], item["motion_prompt"], item["duration"],
         item.get("reference_fingerprint", "first_frame"))
        for item in shots
    ]
    return hashlib.sha256(
        json.dumps({"shots": stable, "ratio": ratio, "audio_mode": audio_mode}, ensure_ascii=False).encode()
    ).hexdigest()


def _matches_planned_clip(snapshot, shot):
    """A completed MP4 is reusable only for its approved motion and duration."""
    if snapshot.get("status") != "completed":
        return False
    # Metadata-less historical clips keep the old compatibility contract.
    # New jobs always persist the exact prompt and requested duration.
    return (
        (not snapshot.get("prompt") or snapshot.get("prompt") == shot["motion_prompt"])
        and (snapshot.get("duration") is None or snapshot.get("duration") == shot["duration"])
        and snapshot.get("reference_fingerprint", "first_frame") == shot.get("reference_fingerprint", "first_frame")
    )


def _current(task_id, segments, ratio=None):
    """Report current shot states. Image/video snapshots must still match."""
    record = _load(task_id)
    state = record.get("status", "not_started")
    if state == "not_started":
        return {"status": "not_started", "total": len(segments), "completed": 0,
                "preferred_audio_mode": audio_preference(task_id)}
    expected = record.get("shots") or []
    actual = {int(s.get("segment_index") or 0): s for s in segments}
    completed = 0
    shots = []
    valid = True
    for shot in expected:
        seg = actual.get(shot["index"])
        if not seg or not seg.get("image_path"):
            item = {"index": shot["index"], "status": "stale"}
            valid = False
        else:
            try:
                signature = shot_video_jobs._identity(seg["image_path"])
            except OSError:
                signature = None
            shot_text = str(seg.get("text") or "")
            current_direction = lookup_shot(task_id, shot["index"], shot_text)
            current_prompt = (compose_motion_prompt(load_plan(task_id) or {},
                                                     shot["index"], current_direction.get("motion_prompt")
                                                     or DEFAULT_MOTION_PROMPT)
                              if current_direction else DEFAULT_MOTION_PROMPT)
            try:
                current_references = h3_references.for_shot(
                    task_id, seg["image_path"], current_direction.get("scene_id") or "",
                )
                actual_refs = (current_references or {}).get("fingerprint", "first_frame")
                current_prompt = compile_video_prompt(
                    task_id, shot_text, current_prompt, current_references, shot_video_jobs._video_config(),
                )
            except (OSError, ValueError):
                actual_refs = "invalid"
            if (signature != shot["signature"] or shot_text != shot.get("text", "")
                    or current_prompt != shot.get("motion_prompt")
                    or actual_refs != shot.get("reference_fingerprint", "first_frame")):
                item = {"index": shot["index"], "status": "stale"}
                valid = False
            else:
                snapshot = shot_video_jobs.status(task_id, shot["index"], seg["image_path"])
                if snapshot.get("status") == "completed" and not _matches_planned_clip(snapshot, shot):
                    item = {"index": shot["index"], "status": "stale"}
                    valid = False
                else:
                    item = {"index": shot["index"], "status": snapshot.get("status", "not_generated")}
        if item["status"] == "completed":
            completed += 1
        item["scene_id"] = shot.get("scene_id") or ""
        item["transition_to_next"] = shot.get("transition_to_next") or "cut"
        shots.append(item)
    if ratio is not None and normalize_ratio(ratio) != record.get("ratio"):
        valid = False
    if len(segments) != len(expected):
        valid = False
    if state == "completed" and record.get("clip_signatures"):
        current_clips = []
        for shot in expected:
            clip = shot_video_jobs.matching_video_path(task_id, {
                "segment_index": shot["index"], "image_path": shot["image"],
            })
            try:
                current_clips.append(shot_video_jobs._identity(clip) if clip else None)
            except OSError:
                current_clips.append(None)
        if current_clips != record["clip_signatures"]:
            valid = False
    if not valid:
        state = "stale"
    elif state in {"generating", "rendering", "transcribing", "subtitling", "verifying"} and task_id not in _ACTIVE:
        state = "interrupted"
    elif state == "completed":
        file = Path(record.get("output_path") or "/nonexistent")
        if not file.is_file():
            state = "failed"
    return {
        "status": state, "total": len(expected), "completed": completed,
        "shots": shots, "duration": record.get("duration"),
        "error": record.get("error") if state in {"failed", "interrupted", "delivery_blocked"} else None,
        "has_video": state == "completed",
        "is_strict_video": True,
        "has_audio": bool(record.get("has_audio")),
        "audio_mode": record.get("audio_mode", "native"),
        "preferred_audio_mode": audio_preference(task_id),
        "job_id": record.get("job_id"),
        "delivery_stage": record.get("delivery_stage"),
        "delivery_error": record.get("delivery_error"),
        "delivery_duration": record.get("delivery_duration"),
        "max_duration": record.get("max_duration"),
        "subtitles": record.get("subtitles", False),
        "native_dialogue": record.get("native_dialogue", False),
        "dialogue_verified": record.get("dialogue_verified", False),
    }


def status(task_id, segments, ratio=None):
    with _LOCK:
        return _current(task_id, segments, ratio)


def _exec(cmd, timeout=900):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise VideoFirstError("FFmpeg 无法处理动态视频素材，请检查编码和文件完整性")
    return result


def _probe(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise VideoFirstError("系统未安装 FFprobe，请安装 FFmpeg")
    report = _exec([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height", "-of", "json", str(path),
    ], timeout=30)
    try:
        data = json.loads(report.stdout)
        streams = data.get("streams") or []
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (ValueError, TypeError):
        raise VideoFirstError("动态片段没有有效的视频轨道") from None
    if duration <= 0.2 or not any(v.get("codec_type") == "video" for v in streams):
        raise VideoFirstError("动态片段没有有效的视频轨道")
    return duration


def _has_audio(path):
    """True when a clip carries its own audio track (e.g. MiniMax-H3)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    return result.returncode == 0 and "audio" in result.stdout


def render_clips(paths, output, ratio="16:9", fps=30, audio_mode="native"):
    from src.api.work_limits import render_slot
    with render_slot:
        return _render_clips(paths, output, ratio=ratio, fps=fps, audio_mode=audio_mode)


def _render_clips(paths, output, ratio="16:9", fps=30, audio_mode="native"):
    """Concat clips with optional provider-audio mute for consistent voiceover later."""
    if audio_mode not in {"native", "mute"}:
        raise VideoFirstError("音频策略只支持 native 或 mute")
    if not paths or any(not Path(path).is_file() for path in paths):
        raise VideoFirstError("有动态视频片段缺失，禁止用图片替代")
    ratio = normalize_ratio(ratio)
    width, height = {"16:9": (1280, 720), "9:16": (720, 1280),
                     "3:4": (720, 960)}[ratio]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoFirstError("找不到 FFmpeg")
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video-first-", dir=destination.parent) as temp:
        temp_dir = Path(temp)
        # A provider that returns native audio (MiniMax-H3) keeps its track;
        # silent providers keep the original video-only render.
        clip_audio = [_has_audio(path) for path in paths]
        keep_audio = audio_mode == "native" and any(clip_audio)
        normalized = []
        for i, path in enumerate(paths):
            _probe(path)
            target = temp_dir / f"scene_{i:04d}.mp4"
            # Normalize geometry, FPS, pixels, codec and stream timebase.
            filters = (
                f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1,format=yuv420p,setpts=PTS-STARTPTS"
            )
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path)]
            if not keep_audio:
                command += ["-map", "0:v:0", "-an"]
            elif clip_audio[i]:
                command += ["-map", "0:v:0", "-map", "0:a:0"]
            else:
                # Keep the concat uniform: silent clips get a matching
                # silent track so every segment has the same stream layout.
                command += [
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                ]
            command += ["-vf", filters, "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "21", "-r", str(fps), "-video_track_timescale", "30000"]
            if keep_audio:
                command += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
            command.append(str(target))
            _exec(command)
            normalized.append(target)
        manifest = temp_dir / "concat.txt"
        manifest.write_text(
            "".join("file '" + str(path).replace("'", "'\\''") + "'\n"
                    for path in normalized),
            encoding="utf-8",
        )
        staged = temp_dir / "assembled.mp4"
        concat = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
            "-safe", "0", "-i", str(manifest), "-map", "0:v:0",
        ]
        concat += ["-map", "0:a:0", "-c:a", "copy"] if keep_audio else ["-an"]
        concat += ["-c:v", "copy", "-movflags", "+faststart", str(staged)]
        _exec(concat)
        final_duration = _probe(staged)
        if final_duration < len(paths) * 1.0:
            raise VideoFirstError("视频拼接异常：总时长过短")
        os.replace(staged, destination)
    return str(destination)


def request_stop(task_id):
    """Stop NEW paid dispatch. Existing remote provider jobs may still bill."""
    with _LOCK:
        if task_id not in _ACTIVE:
            return False
        _STOP.add(task_id)
        token = _TOKENS.get(task_id)
        if token:
            token.cancel()
        return True


def start(task_id, segments, *, ratio="16:9", duration=None,
          confirmed=False, force_new_failed=False, audio_mode=None, subtitles=None, max_duration=None):
    """One explicit approval for up to MAX_SHOTS paid video generations."""
    if not confirmed:
        raise VideoFirstError("请确认所有待生成镜头将按云端视频模型用量计费")
    ratio = normalize_ratio(ratio)
    audio_mode = audio_mode if audio_mode is not None else audio_preference(task_id)
    if audio_mode not in {"native", "mute"}:
        raise VideoFirstError("音频策略不支持此选项")
    duration = shot_video_jobs.default_duration() if duration is None else duration
    from src.media.minimax_i2v import is_h3_model
    config = shot_video_jobs._video_config()
    is_native_h3 = shot_video_jobs.video_provider() == "minimax" and is_h3_model(config.get("model"))
    subtitles = is_native_h3 and audio_mode == "native" if subtitles is None else bool(subtitles)
    if subtitles and audio_mode != "native":
        raise VideoFirstError("静音模式不能生成原生对白字幕")
    max_duration = len(segments) * duration if max_duration is None and is_native_h3 and audio_mode == "native" else max_duration
    if max_duration is not None:
        import math
        if not math.isfinite(float(max_duration)) or not 0 < float(max_duration) <= 3600:
            raise VideoFirstError("总时长必须在 0–3600 秒之间")
        if audio_mode != "native":
            raise VideoFirstError("静音模式不能按原生对白时间轴裁剪，请取消总时长预算")
    local_delivery = subtitles or max_duration is not None
    if local_delivery:
        try:
            native_delivery.preflight()
        except native_delivery.DeliveryError as error:
            raise VideoFirstError(str(error)) from None
    shots = _shot_inputs(task_id, segments, ratio, duration)
    fingerprint = _fingerprint(shots, ratio, audio_mode)
    with _LOCK:
        if task_id in _ACTIVE:
            raise VideoFirstError("当前项目已在生成动态镜头，请稍后")
        existing = _load(task_id)
        if (existing.get("fingerprint") == fingerprint and existing.get("status") == "completed"
                and existing.get("subtitles", False) == subtitles
                and existing.get("max_duration") == max_duration):
            if Path(existing.get("output_path") or "/nonexistent").is_file():
                previous = _current(task_id, segments, ratio)
                if previous.get("status") == "completed":
                    return previous
        if is_native_h3:
            needs_paid = any(not _matches_planned_clip(
                shot_video_jobs.status(task_id, shot["index"], shot["image"]), shot
            ) for shot in shots)
            plan = load_plan(task_id) or {}
            if needs_paid and (plan.get("status") == "failed"
                               or set(plan.get("shots") or {}) != {str(i) for i in range(len(shots))}):
                raise VideoFirstError("导演规划未完整覆盖镜头，请重新规划并审核后再确认付费生成；已有素材已保留")
        record = {
            "job_id": uuid.uuid4().hex,
            "status": "generating", "fingerprint": fingerprint,
            "shots": shots, "duration": duration, "ratio": ratio, "audio_mode": audio_mode,
            "completed": 0, "error": None,
            "subtitles": subtitles, "max_duration": max_duration,
            "native_dialogue": is_native_h3 and audio_mode == "native",
            "output_path": str(_directory(task_id) / "video_first.mp4"),
        }
        from src.api.task_runtime import task_runtime
        cancellation = task_runtime.begin(task_id)
        if cancellation is None:
            raise VideoFirstError("项目正在规划、写入或删除，请稍后重试")
        try:
            _write(task_id, record)
        except Exception:
            task_runtime.finish(task_id, cancellation)
            raise
        _TOKENS[task_id] = cancellation
        _STOP.discard(task_id)
        _ACTIVE.add(task_id)

    def work():
        try:
            paths = []
            cancellation.raise_if_cancelled()
            for shot in shots:
                cancellation.raise_if_cancelled()
                with _LOCK:
                    if task_id in _STOP:
                        raise VideoFirstError("已停止后续付费镜头任务；已提交的任务仍可能产生费用")
                # Never silently start another paid job for an image that
                # changed during generation.
                if shot_video_jobs._identity(shot["image"]) != shot["signature"]:
                    raise VideoFirstError("首帧已修改，停止后续付费请求，请重新确认")
                current = shot_video_jobs.status(task_id, shot["index"], shot["image"])
                if not _matches_planned_clip(current, shot):
                    # An image-only signature is insufficient: a clip made
                    # for an older director prompt must not be silently
                    # inserted into the new approved storyboard.
                    kwargs = {
                        "duration": shot["duration"],
                        "force_new": force_new_failed and current.get("status") == "failed",
                        "cancellation": cancellation,
                    }
                    if shot.get("reference_assets") is not None:
                        kwargs["reference_assets"] = shot["reference_assets"]
                    shot_video_jobs.start(
                        task_id, shot["index"], shot["image"],
                        shot["motion_prompt"], **kwargs,
                    )
                    deadline = time.monotonic() + 1200
                    while time.monotonic() < deadline:
                        cancellation.raise_if_cancelled()
                        with _LOCK:
                            if task_id in _STOP:
                                raise VideoFirstError("已停止后续付费镜头任务；已提交的任务仍可能产生费用")
                        current = shot_video_jobs.status(task_id, shot["index"], shot["image"])
                        if current.get("status") in {"completed", "failed", "stale", "interrupted"}:
                            break
                        time.sleep(2)
                    else:
                        raise VideoFirstError(f"分镜 {shot['index'] + 1} 生成等待超时，可恢复查询")
                if current.get("status") != "completed":
                    raise VideoFirstError(
                        f"分镜 {shot['index'] + 1} 动态视频尚未完成；保留其他已生成片段，"
                        "请查看单镜头状态后恢复"
                    )
                path = shot_video_jobs.matching_video_path(task_id, {
                    "segment_index": shot["index"], "image_path": shot["image"],
                })
                if not path:
                    raise VideoFirstError("分镜视频校验失败，已停止拼接")
                paths.append(path)
                with _LOCK:
                    if _load(task_id).get("job_id") == record["job_id"]:
                        record["completed"] = len(paths)
                        _write(task_id, record)

            # Strictly ALL clips must exist. Audio files and still frames
            # are never consulted by the final MP4 renderer.
            with _LOCK:
                record["status"] = "rendering"
                _write(task_id, record)
            clip_signatures = [shot_video_jobs._identity(path) for path in paths]
            if local_delivery:
                _finish_local_delivery(task_id, record, paths, clip_signatures, cancellation)
                return
            final = (render_clips(paths, record["output_path"], ratio=ratio, audio_mode="mute")
                     if audio_mode == "mute"
                     else render_clips(paths, record["output_path"], ratio=ratio))
            if ([shot_video_jobs._identity(path) for path in paths] != clip_signatures
                    or any(
                        shot_video_jobs._identity(shot["image"]) != shot["signature"]
                        or h3_references.fingerprint_now(
                            task_id, shot["image"], shot.get("scene_id") or ""
                        ) != shot.get("reference_fingerprint", "first_frame")
                        for shot in shots
                    )):
                Path(final).unlink(missing_ok=True)
                raise VideoFirstError("渲染期间首帧已变化，请重新生成")
            with _LOCK:
                record["status"] = "completed"
                record["clip_signatures"] = clip_signatures
                record["has_audio"] = _has_audio(final)
                record["error"] = None
                _write(task_id, record)
        except Exception as error:
            with _LOCK:
                record["status"] = "delivery_blocked" if local_delivery and len(paths) == len(shots) else "failed"
                record["error"] = (
                    str(error) if isinstance(error, VideoFirstError)
                    else "视频生成或拼接失败，已完成的动态镜头仍保留，可重试"
                )
                if _load(task_id).get("job_id") == record["job_id"]:
                    _write(task_id, record)
        finally:
            with _LOCK:
                _ACTIVE.discard(task_id)
                _STOP.discard(task_id)
                _TOKENS.pop(task_id, None)
            task_runtime.finish(task_id, cancellation)

    from src.api.work_limits import generation_pool
    _launch(task_id, work, cancellation, generation_pool)
    return status(task_id, segments, ratio=ratio)


def _launch(task_id, work, cancellation, pool):
    from src.api.task_runtime import task_runtime
    def cancelled():
        # Successful Future.cancel means no worker can touch this record. The
        # runtime may call this while holding its lifecycle lock; do not take
        # _LOCK here (start acquires those two locks in the opposite order).
        record = _load(task_id)
        record.update(status="interrupted", error="任务已停止，已有视频保留，请明确重试本地交付或生产")
        _write(task_id, record)
        _ACTIVE.discard(task_id)
        _STOP.discard(task_id)
        _TOKENS.pop(task_id, None)
        task_runtime.finish(task_id, cancellation)
    try:
        future = pool.submit(work)
        cancellation.bind_future(future, cancelled)
    except RuntimeError:
        cancelled()
        raise VideoFirstError("服务正在关闭，已有素材已保留") from None


def _register_delivery_assets(task_id, metadata):
    from src.database import mysql_client
    for field, kind, label in (("subtitle_path", "subtitle", "原生对白 SRT"),
                                ("vtt_path", "subtitle", "原生对白 VTT"),
                                ("delivery_path", "video", "原生对白成片")):
        path = metadata.get(field)
        if path and Path(path).is_file():
            asset = mysql_client.save_task_asset(task_id, kind, "native_delivery", path=path, label=label)
            if not asset:
                raise native_delivery.DeliveryError("交付资产登记失败，文件已保留，请重试本地交付")


def _finish_local_delivery(task_id, record, paths, identities, cancellation=None):
    def stage(value):
        if cancellation and cancellation.is_cancelled():
            raise native_delivery.DeliveryError("本地交付已停止，原始视频已保留")
        if cancellation:
            cancellation.raise_if_cancelled()
        with _LOCK:
            if task_id in _STOP:
                raise native_delivery.DeliveryError("本地交付已停止，原始视频已保留")
            if ([shot_video_jobs._identity(path) for path in paths] != identities
                    or any(shot_video_jobs._identity(s["image"]) != s["signature"]
                           or h3_references.fingerprint_now(task_id, s["image"], s.get("scene_id") or "")
                           != s.get("reference_fingerprint", "first_frame") for s in record["shots"])):
                raise native_delivery.DeliveryError("交付期间源素材已变化，原始文件已保留，请检查版本")
            record["status"] = "verifying" if value == "publishing" else value
            record["delivery_stage"] = value
            _write(task_id, record)
    try:
        from src.api.work_limits import render_slot
        with render_slot:
            metadata = native_delivery.deliver(
                paths, record["shots"], record["output_path"], ratio=record["ratio"],
                max_duration=record.get("max_duration"), subtitles=record.get("subtitles", True),
                stage_callback=stage,
                asset_callback=lambda metadata: _register_delivery_assets(task_id, metadata),
            )
        _register_delivery_assets(task_id, metadata)
        if cancellation and cancellation.is_cancelled():
            raise native_delivery.DeliveryError("本地交付已停止，已有文件已保留")
        with _LOCK:
            record.update(metadata)
            record.update(status="completed", delivery_stage="completed", delivery_error=None,
                          error=None, clip_signatures=identities)
            _write(task_id, record)
    except native_delivery.DeliveryError as error:
        with _LOCK:
            record.update(status="delivery_blocked", delivery_error=str(error), error=str(error),
                          clip_signatures=identities)
            _write(task_id, record)


def retry_delivery(task_id, segments, *, ratio="16:9", subtitles=True, max_duration=None):
    """Only local work. Never replan or submit a paid provider job."""
    with _LOCK:
        if task_id in _ACTIVE:
            raise VideoFirstError("当前项目正在生产，请稍后")
        record = _load(task_id)
        if not record.get("shots") or record.get("audio_mode", "native") != "native":
            raise VideoFirstError("没有可交付的原生音轨任务")
        current = _current(task_id, segments, ratio)
        if current["status"] == "stale" or current["completed"] != len(record["shots"]):
            raise VideoFirstError("原始镜头未全部完成或已过期，请检查素材；不会自动提交付费任务")
        if max_duration is None:
            max_duration = record.get("max_duration") or record.get("duration", 5)*len(record["shots"])
        try:
            native_delivery.preflight()
        except native_delivery.DeliveryError as error:
            raise VideoFirstError(str(error)) from None
        paths = [shot_video_jobs.matching_video_path(task_id, {"segment_index":s["index"], "image_path":s["image"]})
                 for s in record["shots"]]
        if any(not path or not Path(path).is_file() for path in paths):
            raise VideoFirstError("原始动态镜头缺失，不能继续本地交付")
        identities = [shot_video_jobs._identity(path) for path in paths]
        record.update(status="transcribing", delivery_stage="transcribing", subtitles=bool(subtitles),
                      max_duration=max_duration, error=None, delivery_error=None, delivery_duration=None, dialogue_verified=False,
                      native_dialogue=True)
        from src.api.task_runtime import task_runtime
        cancellation = task_runtime.begin(task_id)
        if cancellation is None:
            raise VideoFirstError("项目正在规划、写入或删除，请稍后重试")
        try:
            _write(task_id, record)
        except Exception:
            task_runtime.finish(task_id, cancellation)
            raise
        _TOKENS[task_id] = cancellation
        _STOP.discard(task_id)
        _ACTIVE.add(task_id)
    def work():
        try:
            _finish_local_delivery(task_id, record, paths, identities, cancellation)
        except Exception:
            with _LOCK:
                record.update(status="delivery_blocked", delivery_error="本地交付异常，原始视频已保留", error="本地交付异常，原始视频已保留")
                _write(task_id, record)
        finally:
            with _LOCK:
                _ACTIVE.discard(task_id)
                _STOP.discard(task_id)
                _TOKENS.pop(task_id, None)
            task_runtime.finish(task_id, cancellation)
    from src.api.work_limits import export_pool
    _launch(task_id, work, cancellation, export_pool)
    return status(task_id, segments, ratio=ratio)


def completed_path(task_id, segments, ratio=None):
    if status(task_id, segments, ratio=ratio).get("status") != "completed":
        return None
    return _load(task_id).get("output_path")
