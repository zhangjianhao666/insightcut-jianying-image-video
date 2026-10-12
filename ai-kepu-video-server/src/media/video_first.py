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
from src.media import shot_video_jobs
from src.text.storyboard_director import DEFAULT_MOTION_PROMPT, lookup_shot
from src.utils.rendering import normalize_ratio

MAX_SHOTS = 12
_ACTIVE = set()
_STOP = set()
_LOCK = threading.RLock()


class VideoFirstError(ValueError):
    pass


def _directory(task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(task_id)):
        raise VideoFirstError("非法项目 ID")
    return Config.BASE_DIR / "data" / "media" / "_video_first" / str(task_id)


def _record_path(task_id):
    return _directory(task_id) / "job.json"


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
    for segment in sorted(segments, key=lambda row: int(row.get("segment_index") or 0)):
        index = int(segment.get("segment_index") or 0)
        image = segment.get("image_path")
        if segment.get("image_status") != "completed" or not image or not Path(image).is_file():
            raise VideoFirstError(f"第 {index + 1} 个分镜的首帧图片尚未完成")
        direction = lookup_shot(task_id, index, segment.get("text") or "")
        motion_prompt = str(direction.get("motion_prompt") or "").strip()
        if not motion_prompt:
            motion_prompt = DEFAULT_MOTION_PROMPT
        result.append({
            "index": index,
            "image": str(Path(image).resolve()),
            "signature": shot_video_jobs._identity(image),
            "text": str(segment.get("text") or ""),
            "motion_prompt": motion_prompt[:1800],
            "duration": duration,
        })
    return result


def _fingerprint(shots, ratio):
    stable = [
        (item["index"], item["signature"], item["motion_prompt"], item["duration"])
        for item in shots
    ]
    return hashlib.sha256(
        json.dumps({"shots": stable, "ratio": ratio}, ensure_ascii=False).encode()
    ).hexdigest()


def _current(task_id, segments, ratio=None):
    """Report current shot states. Image/video snapshots must still match."""
    record = _load(task_id)
    state = record.get("status", "not_started")
    if state == "not_started":
        return {"status": "not_started", "total": len(segments), "completed": 0}
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
            if signature != shot["signature"] or str(seg.get("text") or "") != shot.get("text", ""):
                item = {"index": shot["index"], "status": "stale"}
                valid = False
            else:
                snapshot = shot_video_jobs.status(task_id, shot["index"], seg["image_path"])
                item = {"index": shot["index"], "status": snapshot.get("status", "not_generated")}
        if item["status"] == "completed":
            completed += 1
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
    elif state in {"generating", "rendering"} and task_id not in _ACTIVE:
        state = "interrupted"
    elif state == "completed":
        file = Path(record.get("output_path") or "/nonexistent")
        if not file.is_file():
            state = "failed"
    return {
        "status": state, "total": len(expected), "completed": completed,
        "shots": shots, "duration": record.get("duration"),
        "error": record.get("error") if state in {"failed", "interrupted"} else None,
        "has_video": state == "completed",
        "is_strict_video": True,
        "has_audio": bool(record.get("has_audio")),
        "job_id": record.get("job_id"),
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


def render_clips(paths, output, ratio="16:9", fps=30):
    """Concat clips in order; keep provider audio when the model produced it."""
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
        keep_audio = any(clip_audio)
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
        return True


def start(task_id, segments, *, ratio="16:9", duration=None,
          confirmed=False, force_new_failed=False):
    """One explicit approval for up to MAX_SHOTS paid video generations."""
    if not confirmed:
        raise VideoFirstError("请确认所有待生成镜头将按云端视频模型用量计费")
    ratio = normalize_ratio(ratio)
    duration = shot_video_jobs.default_duration() if duration is None else duration
    shots = _shot_inputs(task_id, segments, ratio, duration)
    fingerprint = _fingerprint(shots, ratio)
    with _LOCK:
        if task_id in _ACTIVE:
            raise VideoFirstError("当前项目已在生成动态镜头，请稍后")
        existing = _load(task_id)
        if existing.get("fingerprint") == fingerprint and existing.get("status") == "completed":
            if Path(existing.get("output_path") or "/nonexistent").is_file():
                previous = _current(task_id, segments, ratio)
                if previous.get("status") == "completed":
                    return previous
        record = {
            "job_id": uuid.uuid4().hex,
            "status": "generating", "fingerprint": fingerprint,
            "shots": shots, "duration": duration, "ratio": ratio,
            "completed": 0, "error": None,
            "output_path": str(_directory(task_id) / "video_first.mp4"),
        }
        _write(task_id, record)
        _STOP.discard(task_id)
        _ACTIVE.add(task_id)

    def work():
        try:
            paths = []
            for shot in shots:
                with _LOCK:
                    if task_id in _STOP:
                        raise VideoFirstError("已停止后续付费镜头任务；已提交的任务仍可能产生费用")
                # Never silently start another paid job for an image that
                # changed during generation.
                if shot_video_jobs._identity(shot["image"]) != shot["signature"]:
                    raise VideoFirstError("首帧已修改，停止后续付费请求，请重新确认")
                current = shot_video_jobs.status(task_id, shot["index"], shot["image"])
                if current.get("status") != "completed":
                    shot_video_jobs.start(
                        task_id, shot["index"], shot["image"],
                        shot["motion_prompt"], duration=shot["duration"],
                        force_new=force_new_failed and current.get("status") == "failed",
                    )
                    deadline = time.monotonic() + 1200
                    while time.monotonic() < deadline:
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
            final = render_clips(paths, record["output_path"], ratio=ratio)
            if ([shot_video_jobs._identity(path) for path in paths] != clip_signatures
                    or any(shot_video_jobs._identity(shot["image"]) != shot["signature"]
                   for shot in shots)):
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
                record["status"] = "failed"
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

    threading.Thread(target=work, name=f"video-first-{task_id}", daemon=True).start()
    return status(task_id, segments, ratio=ratio)


def completed_path(task_id, segments, ratio=None):
    if status(task_id, segments, ratio=ratio).get("status") != "completed":
        return None
    return _load(task_id).get("output_path")
