"""Wan 2.7 text-to-image adapter for Beijing Model Studio workspaces.

Generates one PNG synchronously and archives bytes locally. Never puts an API
key in logs or reports; never sends a paid batch without the normal project
asset-generation action.
"""
import base64
import io
import os
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, UnidentifiedImageError


class WanImageError(RuntimeError):
    """A stable credential-safe provider error."""


def normalize_api_base(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    allowed = (
        host in {
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
        }
        or host.endswith(".maas.aliyuncs.com")
    )
    if (parsed.scheme != "https" or not allowed or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise WanImageError("图片服务必须使用官方百炼 HTTPS 地址")
    if parsed.path in ("", "/", "/compatible-mode/v1"):
        return f"https://{host}/api/v1"
    if parsed.path == "/api/v1":
        return f"https://{host}/api/v1"
    raise WanImageError("图片 API 地址应填写业务空间根地址或 /api/v1")


def image_size(width: int, height: int, requested: str = "auto") -> str:
    """Explicit dimensions preserve image-to-video aspect ratio (Wan 2.7)."""
    presets = {
        "16:9": "1280*720",
        "9:16": "720*1280",
        "3:4": "960*1280",
        "4:3": "1280*960",
        "1:1": "1024*1024",
    }
    if requested and requested != "auto":
        if requested in presets:
            return presets[requested]
        import re
        match = re.fullmatch(r"(\d{3,4})[x*](\d{3,4})", str(requested))
        if match:
            w, h = map(int, match.groups())
            pixels = w * h
            if 768 * 768 <= pixels <= 2048 * 2048 and 1 / 8 <= w / h <= 8:
                return f"{w}*{h}"
        raise WanImageError("万相尺寸必须符合 768² 至 2048² 总像素范围")
    if width <= 0 or height <= 0:
        raise WanImageError("无效的画布尺寸")
    ratio = width / height
    if ratio >= 1.5:
        return presets["16:9"]
    if ratio <= 0.66:
        return presets["9:16"]
    if ratio < 0.88:
        return presets["3:4"]
    if ratio > 1.13:
        return presets["4:3"]
    return presets["1:1"]


class WanImageGenerator:
    def __init__(self, api_key: str, base_url: str, model="wan2.7-image", session=None):
        if not api_key:
            raise WanImageError("请在本地配置百炼生图 API Key")
        if model not in {"wan2.7-image", "wan2.7-image-pro"}:
            raise WanImageError("当前生图适配器仅支持 wan2.7-image 系列")
        self.api_key = api_key
        self.base_url = normalize_api_base(base_url)
        self.model = model
        self.session = session or requests.Session()

    @staticmethod
    def _extract_image_url(data):
        for choice in (data.get("output") or {}).get("choices") or []:
            for item in (choice.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("image"):
                    return str(item["image"])
        raise WanImageError("百炼未返回生成图片地址")

    @staticmethod
    def _reference_data_url(path):
        picture = Path(path).resolve()
        if not picture.is_file() or picture.stat().st_size > 20 * 1024 * 1024:
            raise WanImageError("IP 参考形象图片不存在或超过 20MB")
        # Validate actual image type before embedding; no arbitrary local media.
        try:
            with Image.open(picture) as image:
                image.verify()
            with Image.open(picture) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise WanImageError("IP 参考图格式不支持")
                image.load()
                memory = io.BytesIO()
                image.convert("RGB").save(memory, format="JPEG", quality=90)
        except (OSError, UnidentifiedImageError):
            raise WanImageError("IP 参考图损坏") from None
        return "data:image/jpeg;base64," + base64.b64encode(memory.getvalue()).decode("ascii")

    def submit(self, prompt: str, size: str, reference_image=None) -> str:
        if not prompt.strip() or len(prompt) > 5000:
            raise WanImageError("文生图提示词应为 1–5000 个字符")
        content = [{"text": prompt}]
        if reference_image:
            # Wan 2.7 can preserve an IP's look through multimodal image
            # reference input. Text-only mode remains unchanged.
            content[0]["text"] = (
                "以输入参考图中的同一角色为固定系列IP：锁定脸型、毛色、"
                "服饰、人物比例与主要视觉特征。改变场景和动作，但不要改变IP身份。"
                + prompt
            )[:5000]
            content.append({"image": self._reference_data_url(reference_image)})
        payload = {
            "model": self.model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": {"size": size, "n": 1, "watermark": False},
        }
        try:
            response = self.session.post(
                f"{self.base_url}/services/aigc/multimodal-generation/generation",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(15, 180),
            )
            response.raise_for_status()
            return self._extract_image_url(response.json())
        except (requests.RequestException, ValueError) as exc:
            raise WanImageError("万相生图调用失败，请检查模型权限、额度、地域及网络") from None

    @staticmethod
    def _is_trusted_image_url(raw: str) -> bool:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and (host.endswith(".aliyuncs.com")
                 or host.endswith(".aliyuncs.com.cn")
                 or host.endswith(".alicdn.com"))
        )

    def save(self, image_url: str, output_path: str) -> str:
        if not self._is_trusted_image_url(image_url):
            raise WanImageError("图片地址不是可信的阿里云 HTTPS 输出地址")
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".download")
        try:
            with self.session.get(
                image_url, timeout=(10, 100), stream=True, allow_redirects=False
            ) as response:
                response.raise_for_status()
                if response.status_code >= 300:
                    raise WanImageError("下载图片时遇到不允许的重定向")
                content = bytearray()
                for part in response.iter_content(1024 * 1024):
                    content.extend(part)
                    if len(content) > 25 * 1024 * 1024:
                        raise WanImageError("图片超过 25MB 保存上限")
            # Verify actual media bytes instead of trusting the remote Content-Type.
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise WanImageError("百炼返回了不支持的图片格式")
                image.load()
                image.convert("RGB").save(temporary, format="PNG")
            os.replace(temporary, destination)
            return str(destination)
        except (requests.RequestException, OSError, UnidentifiedImageError) as exc:
            raise WanImageError("百炼生图素材下载或校验失败") from None
        finally:
            temporary.unlink(missing_ok=True)

    def generate(self, prompt: str, size: str, output_path: str, reference_image=None) -> str:
        url = self.submit(prompt, size, reference_image=reference_image)
        return self.save(url, output_path)
