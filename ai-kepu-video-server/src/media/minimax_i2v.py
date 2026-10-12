"""MiniMax Hailuo image-to-video HTTP adapter.

Only explicit user requests submit paid jobs. A submitted task ID is
persisted by the caller before polling so a restart never silently
resubmits a job.
"""
import base64
import ipaddress
import mimetypes
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


class MinimaxVideoError(RuntimeError):
    """A credential-safe failure returned to the caller."""


def _is_public_https(url):
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
    )


H3_MODEL_PREFIX = "MiniMax-H3"


def is_h3_model(model):
    """MiniMax-H3 speaks the /v2 protocol and is the audio-capable family."""
    return str(model or "").strip().startswith(H3_MODEL_PREFIX)


class MinimaxImageToVideo:
    """MiniMax image-to-video for both the v1 and the H3 v2 protocols.

    ``MiniMax-Hailuo-2.3`` is silent and uses /v1/video_generation.
    ``MiniMax-H3`` / ``MiniMax-H3-Max`` use /v2/video_generation, take a
    multimodal ``content`` array and return a clip that carries its own AAC
    audio track.
    """

    DEFAULT_BASE_URL = "https://api.minimax.cn"
    DEFAULT_MODEL = "MiniMax-Hailuo-2.3"
    DEFAULT_RESOLUTION = "1080P"
    DEFAULT_DURATION = 6
    H3_RESOLUTIONS = ("768P", "2K")
    H3_MAX_RESOLUTIONS = ("480P", "768P")
    H3_DEFAULT_RESOLUTION = "768P"
    H3_DEFAULT_DURATION = 6
    H3_DURATIONS = tuple(range(4, 16))
    # MiniMax-Hailuo-2.3 accepts 1080P only at 6s; 768P also allows 10s.
    DURATION_BY_RESOLUTION = {
        "512P": (6, 10),
        "720P": (6,),
        "768P": (6, 10),
        "1080P": (6,),
    }
    RATIOS = ((21 / 9, "21:9"), (16 / 9, "16:9"), (4 / 3, "4:3"),
              (1.0, "1:1"), (3 / 4, "3:4"), (9 / 16, "9:16"))
    ERROR_HINTS = {
        1000: "服务暂时不可用，请稍后重试",
        1002: "请求过于频繁，请稍后重试",
        1004: "认证失败，请检查 MiniMax API Key",
        1008: "账户余额不足，请先充值",
        1013: "服务内部错误，请稍后重试",
        1026: "提示词包含敏感内容，请调整后重试",
        1027: "生成内容包含敏感内容，请调整提示词后重试",
        2013: "请求参数不合法，请检查模型、时长与分辨率",
        2049: "API Key 无效，请检查 MiniMax 配置",
    }

    def __init__(self, api_key=None, base_url=None, model=None,
                 resolution=None, session=None):
        from src.config import Config
        video = Config.load_model_config().get("video") or {}

        self.api_key = str(
            api_key or os.environ.get("MINIMAX_API_KEY")
            or video.get("api_key") or ""
        ).strip()
        self.base_url = self._normalize_base_url(
            base_url or os.environ.get("MINIMAX_BASE_URL")
            or video.get("base_url") or self.DEFAULT_BASE_URL
        )
        self.model = str(
            model or os.environ.get("MINIMAX_VIDEO_MODEL")
            or video.get("model") or self.DEFAULT_MODEL
        ).strip()
        self.resolution = str(
            resolution or os.environ.get("MINIMAX_VIDEO_RESOLUTION")
            or video.get("resolution") or ""
        ).strip().upper()
        if self.resolution not in self.resolution_options(self.model):
            self.resolution = self.default_resolution(self.model)
        self.session = session or requests.Session()
        if not self.api_key:
            raise MinimaxVideoError("请先在设置页配置 MiniMax（海螺）API Key")

    @staticmethod
    def _normalize_base_url(raw_base):
        parsed = urlparse(str(raw_base or "").strip())
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise MinimaxVideoError("请配置 MiniMax 官方 HTTPS 服务地址")
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        if path not in {"", "/"}:
            raise MinimaxVideoError("MiniMax 服务地址只需填 https://api.minimax.cn")
        return f"https://{parsed.netloc}"

    @classmethod
    def resolution_options(cls, model=None):
        if str(model or cls.DEFAULT_MODEL).strip() == "MiniMax-H3-Max":
            return cls.H3_MAX_RESOLUTIONS
        if is_h3_model(model or cls.DEFAULT_MODEL):
            return cls.H3_RESOLUTIONS
        return ("768P", "1080P")

    @classmethod
    def default_resolution(cls, model=None):
        if is_h3_model(model or cls.DEFAULT_MODEL):
            return cls.H3_DEFAULT_RESOLUTION
        return cls.DEFAULT_RESOLUTION

    @classmethod
    def duration_options(cls, resolution=None, model=None):
        if str(model or cls.DEFAULT_MODEL).strip() == "MiniMax-H3-Max":
            return tuple(range(5, 16))
        if is_h3_model(model or cls.DEFAULT_MODEL):
            return cls.H3_DURATIONS
        value = str(resolution or cls.DEFAULT_RESOLUTION).strip().upper()
        return cls.DURATION_BY_RESOLUTION.get(value, (cls.DEFAULT_DURATION,))

    @classmethod
    def default_duration(cls, model=None):
        return cls.H3_DEFAULT_DURATION if is_h3_model(model or cls.DEFAULT_MODEL) else cls.DEFAULT_DURATION

    @classmethod
    def supports_audio(cls, model=None):
        """Only the H3 family returns a clip with its own audio track."""
        return is_h3_model(model or cls.DEFAULT_MODEL)

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _describe(self, body, fallback):
        """Turn either protocol's error payload into one sanitised sentence."""
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("type") or "").strip()
        else:
            message = str((body.get("base_resp") or {}).get("status_msg") or "").strip()
        code = None
        match = re.search(r"\((\d{4})\)\s*$", message)
        if match:
            code = int(match.group(1))
            message = message[:match.start()].strip().rstrip(",，")
        hint = self.ERROR_HINTS.get(code)
        if hint and message:
            return f"{hint}：{message[:220]}"
        return hint or message[:220] or fallback

    @classmethod
    def _ratio_for_image(cls, image_path):
        """H3 wants an explicit ratio; the first frame is the source of truth."""
        try:
            from PIL import Image
            with Image.open(image_path) as handle:
                width, height = handle.size
        except Exception:
            return "16:9"
        if not width or not height:
            return "16:9"
        value = width / height
        return min(cls.RATIOS, key=lambda item: abs(item[0] - value))[1]

    def _check_base_resp(self, body, fallback):
        base = body.get("base_resp") or {}
        code = base.get("status_code")
        if code in (None, 0):
            return
        hint = self.ERROR_HINTS.get(code)
        if hint:
            raise MinimaxVideoError(hint)
        detail = str(base.get("status_msg") or "").strip()
        raise MinimaxVideoError(f"{fallback}：{detail}" if detail else fallback)

    @staticmethod
    def _data_url(image_path):
        path = Path(image_path)
        if not path.is_file():
            raise MinimaxVideoError("首帧图片不存在")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise MinimaxVideoError("首帧图片超过 20MB 限制")
        mime = mimetypes.guess_type(path.name)[0]
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise MinimaxVideoError("海螺只支持 JPG、PNG、WEBP 格式首帧")
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    @staticmethod
    def _audio_data_url(path):
        sample = Path(path)
        if not sample.is_file() or sample.suffix.lower() != ".wav":
            raise MinimaxVideoError("H3 声音参考必须是本地 WAV 文件")
        if sample.stat().st_size > 8 * 1024 * 1024:
            raise MinimaxVideoError("声音参考不得超过 8 MB")
        return "data:audio/wav;base64," + base64.b64encode(sample.read_bytes()).decode("ascii")

    def _payload(self, image_path, text, duration, reference_assets=None):
        if reference_assets is not None:
            if not is_h3_model(self.model):
                raise MinimaxVideoError("只有 MiniMax-H3 系列支持多模态参考生成")
            if reference_assets.get("mode") != "reference":
                raise MinimaxVideoError("参考模式配置错误")
            images = reference_assets.get("images") or []
            audio = reference_assets.get("audio")
            if not images or len(images) > 9:
                raise MinimaxVideoError("H3 参考模式须提供 1–9 张参考图片")
            content = [{"type": "text", "text": (
                "Reference Image 1 is this shot's intended composition, NOT an exact first frame. "
                "Keep the character and setting consistent with other reference images; "
                "if Audio 1 exists, use its vocal timbre while following the script. "
                + text
            )[:7000]}]
            for image in images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": self._data_url(image)},
                    "role": "reference_image",
                })
            if audio:
                content.append({
                    "type": "audio_url",
                    "audio_url": {"url": self._audio_data_url(audio)},
                    "role": "reference_audio",
                })
            payload = {
                "model": self.model,
                "content": content,
                "duration": int(duration),
                "resolution": self.resolution,
                "ratio": self._ratio_for_image(image_path),
            }
            # API body limit is 64 MB. Minimize memory pressure and fail
            # BEFORE the paid task is created, never silently drop a reference.
            import json
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 64 * 1024 * 1024:
                raise MinimaxVideoError("H3 参考素材请求体超过 64 MB，请缩小图片或音频")
            return "/v2/video_generation", payload

        frame = self._data_url(image_path)
        if is_h3_model(self.model):
            # H3 takes a multimodal content array and needs an explicit ratio.
            return "/v2/video_generation", {
                "model": self.model,
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": frame}, "role": "first_frame"},
                ],
                "duration": int(duration),
                "resolution": self.resolution,
                "ratio": self._ratio_for_image(image_path),
            }
        return "/v1/video_generation", {
            "model": self.model,
            "prompt": text,
            "first_frame_image": frame,
            "duration": int(duration),
            "resolution": self.resolution,
            "prompt_optimizer": True,
        }

    def submit(self, image_path, prompt, duration=None, reference_assets=None):
        text = str(prompt or "").strip()
        if not text:
            raise MinimaxVideoError("视频动作提示词不能为空")
        if len(text) > 2000:
            raise MinimaxVideoError("海螺提示词不能超过 2000 字")
        duration = self.default_duration(self.model) if duration is None else duration
        options = self.duration_options(self.resolution, self.model)
        if duration not in options:
            raise MinimaxVideoError(
                f"海螺 {self.model} 在 {self.resolution} 下只支持 "
                + "、".join(f"{value} 秒" for value in options)
            )
        path, payload = self._payload(image_path, text, duration, reference_assets=reference_assets)
        try:
            # H3 reports failures as HTTP 4xx with an error object, while the
            # v1 protocol answers 200 with base_resp.status_code.
            resp = self.session.post(
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
                timeout=(10, 120),
            )
            body = resp.json() if resp.content else {}
        except (requests.RequestException, ValueError) as exc:
            raise MinimaxVideoError("海螺任务提交失败，请检查网络、额度和模型权限") from exc
        if resp.status_code >= 400:
            raise MinimaxVideoError(
                self._describe(body, "海螺任务提交失败，请检查模型与参数配置")
            )
        self._check_base_resp(body, "海螺任务提交失败，请检查模型权限")
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise MinimaxVideoError("海螺未返回任务 ID，请检查模型权限")
        return task_id

    def _file_url(self, file_id):
        if not file_id:
            raise MinimaxVideoError("海螺任务成功但未返回文件 ID")
        try:
            resp = self.session.get(
                f"{self.base_url}/v1/files/retrieve",
                params={"file_id": file_id},
                headers=self._headers(),
                timeout=(10, 60),
            )
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise MinimaxVideoError("海螺视频地址获取失败，请稍后重试") from exc
        self._check_base_resp(body, "海螺视频地址获取失败")
        url = (body.get("file") or {}).get("download_url")
        if not url:
            raise MinimaxVideoError("海螺未返回视频下载地址")
        return url

    def wait(self, task_id, timeout=900, interval=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = self.session.get(
                    f"{self.base_url}/v1/query/video_generation",
                    params={"task_id": task_id},
                    headers=self._headers(),
                    timeout=(10, 30),
                )
                resp.raise_for_status()
                body = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise MinimaxVideoError("视频任务状态查询失败，可使用已保存的任务 ID 重试查询") from exc
            self._check_base_resp(body, "查询海螺任务状态失败")
            status = body.get("status")
            if status == "Success":
                return self._file_url(body.get("file_id"))
            if status == "Fail":
                raise MinimaxVideoError("海螺视频生成失败，请调整动作提示词后重试")
            if status not in {"Preparing", "Queueing", "Processing"}:
                raise MinimaxVideoError("海螺返回了未知任务状态")
            time.sleep(interval)
        raise MinimaxVideoError("等待视频超时；已保存任务 ID，重新发起时会优先查询原任务")

    def download(self, url, output_path):
        # The provider returns a short-lived signed URL; refuse anything that
        # is not plain public HTTPS so a bad response cannot reach local or
        # private endpoints.
        if not _is_public_https(url):
            raise MinimaxVideoError("视频下载地址不是安全的公网 HTTPS 地址")
        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_suffix(".download")
        try:
            with self.session.get(url, stream=True, timeout=(10, 120),
                                  allow_redirects=False) as resp:
                resp.raise_for_status()
                if 300 <= resp.status_code < 400:
                    raise MinimaxVideoError("视频下载发生未知重定向")
                count = 0
                with temporary.open("wb") as handle:
                    for chunk in resp.iter_content(1024 * 1024):
                        count += len(chunk)
                        if count > 500 * 1024 * 1024:
                            raise MinimaxVideoError("视频文件超过下载上限")
                        handle.write(chunk)
            with temporary.open("rb") as handle:
                header = handle.read(12)
            if len(header) < 12 or header[4:8] != b"ftyp":
                raise MinimaxVideoError("下载内容不是有效 MP4")
            os.replace(temporary, dest)
            return str(dest)
        except requests.RequestException as exc:
            raise MinimaxVideoError("海螺视频文件下载失败，请稍后重试") from exc
        finally:
            temporary.unlink(missing_ok=True)
