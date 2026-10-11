"""Aliyun Wan 2.6 image-to-video HTTP adapter.

Only explicit user requests submit paid jobs. A submitted task ID is persisted
by the caller before polling so a restart never silently resubmits a job.
"""
import base64
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


class WanVideoError(RuntimeError):
    """A credential-safe failure returned to the caller."""


class WanImageToVideo:
    def __init__(self, api_key=None, base_url=None, model=None, session=None):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        # A Beijing workspace provides OpenAI-compatible chat at
        # /compatible-mode/v1 and Wan's async API at /api/v1.
        # Explicit DASHSCOPE_BASE_URL wins over the chat endpoint.
        raw_base = (
            base_url
            or os.environ.get("DASHSCOPE_BASE_URL")
            or os.environ.get("LLM_BASE_URL")
            or "https://dashscope.aliyuncs.com/api/v1"
        ).rstrip("/")
        parsed = urlparse(raw_base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not (
                parsed.hostname == "dashscope.aliyuncs.com"
                or parsed.hostname.endswith(".maas.aliyuncs.com")
                or parsed.hostname == "dashscope-intl.aliyuncs.com"
                or parsed.hostname == "dashscope-us.aliyuncs.com"
            )
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise WanVideoError("请配置官方百炼 HTTPS 服务地址")
        path = parsed.path
        if path == "/compatible-mode/v1":
            path = "/api/v1"
        elif path in ("", "/"):
            path = "/api/v1"
        elif path != "/api/v1":
            raise WanVideoError("万相 API 必须使用 /api/v1 路径")
        self.base_url = f"https://{parsed.netloc}{path}"
        self.model = model or os.environ.get("WAN_I2V_MODEL", "wan2.6-i2v-flash")
        self.session = session or requests.Session()
        if not self.api_key:
            raise WanVideoError("请先在本地 .env 配置 DASHSCOPE_API_KEY")

    def _headers(self, *, async_request=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if async_request:
            headers["X-DashScope-Async"] = "enable"
        return headers

    @staticmethod
    def _data_url(image_path):
        path = Path(image_path)
        if not path.is_file():
            raise WanVideoError("首帧图片不存在")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise WanVideoError("首帧图片超过 20MB 限制")
        mime = mimetypes.guess_type(path.name)[0]
        if mime not in {"image/jpeg", "image/png", "image/webp", "image/bmp"}:
            raise WanVideoError("只支持 JPG、PNG、WEBP、BMP 格式首帧")
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    def submit(self, image_path, prompt, duration=5):
        if not str(prompt).strip():
            raise WanVideoError("视频动作提示词不能为空")
        if not isinstance(duration, int) or not 2 <= duration <= 15:
            raise WanVideoError("万相 2.6 只支持 2 至 15 秒整数时长")
        payload = {
            "model": self.model,
            "input": {"prompt": str(prompt).strip(), "img_url": self._data_url(image_path)},
            "parameters": {
                "duration": duration,
                "resolution": "720P",
                "audio": False,
                "prompt_extend": True,
                "watermark": False,
            },
        }
        try:
            resp = self.session.post(
                f"{self.base_url}/services/aigc/video-generation/video-synthesis",
                headers=self._headers(async_request=True),
                json=payload,
                timeout=(10, 90),
            )
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise WanVideoError("万相任务提交失败，请检查地域、模型权限、额度和网络") from exc
        task_id = (body.get("output") or {}).get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise WanVideoError("万相未返回任务 ID，请检查模型权限")
        return task_id

    def wait(self, task_id, timeout=900, interval=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = self.session.get(
                    f"{self.base_url}/tasks/{task_id}",
                    headers=self._headers(),
                    timeout=(10, 30),
                )
                resp.raise_for_status()
                output = resp.json().get("output") or {}
            except (requests.RequestException, ValueError) as exc:
                raise WanVideoError("视频任务状态查询失败，可使用已保存的任务 ID 重试查询") from exc
            status = output.get("task_status")
            if status == "SUCCEEDED":
                url = output.get("video_url")
                if not url:
                    raise WanVideoError("万相任务成功但未返回视频地址")
                return url
            if status in {"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}:
                raise WanVideoError("万相视频生成未成功，请调整动作提示词后重试")
            if status not in {"PENDING", "RUNNING", "SUSPENDED"}:
                raise WanVideoError("万相返回了未知任务状态")
            time.sleep(interval)
        raise WanVideoError("等待视频超时；已保存任务 ID，重新发起时会优先查询原任务")

    def download(self, url, output_path):
        parsed = urlparse(url)
        # The provider returns a short-lived OSS URL; do not follow arbitrary
        # redirects to local/private endpoints when downloading model output.
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname.endswith(".aliyuncs.com")
            or parsed.hostname.endswith(".aliyuncs.com.cn")
        ):
            raise WanVideoError("视频下载地址不在允许的阿里云域名内")
        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_suffix(".download")
        try:
            with self.session.get(url, stream=True, timeout=(10, 120), allow_redirects=False) as resp:
                resp.raise_for_status()
                if 300 <= resp.status_code < 400:
                    raise WanVideoError("视频下载发生未知重定向")
                count = 0
                with temporary.open("wb") as handle:
                    for chunk in resp.iter_content(1024 * 1024):
                        count += len(chunk)
                        if count > 500 * 1024 * 1024:
                            raise WanVideoError("视频文件超过下载上限")
                        handle.write(chunk)
            with temporary.open("rb") as handle:
                header = handle.read(12)
            if len(header) < 12 or header[4:8] != b"ftyp":
                raise WanVideoError("下载内容不是有效 MP4")
            os.replace(temporary, dest)
            return str(dest)
        except requests.RequestException as exc:
            raise WanVideoError("万相视频文件下载失败，请稍后重试") from exc
        finally:
            temporary.unlink(missing_ok=True)
