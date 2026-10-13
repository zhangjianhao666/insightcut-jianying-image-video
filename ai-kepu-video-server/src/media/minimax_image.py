"""Official MiniMax Image-01, local character references and local output."""
import base64
import io
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image


class MiniMaxImageError(RuntimeError):
    pass


class MiniMaxImageGenerator:
    def __init__(self, api_key, api_url, model="image-01", session=None):
        parsed = urlparse(api_url.rstrip("/"))
        if (parsed.scheme != "https" or parsed.hostname not in {
            "api.minimax.cn", "api.minimaxi.com", "api.minimax.io"
        } or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/v1/image_generation")):
            raise MiniMaxImageError("请使用 MiniMax 官方 HTTPS 生图接口")
        if model not in {"image-01", "image-01-live"} or not api_key:
            raise MiniMaxImageError("MiniMax 图片模型或密钥未配置")
        self.url = api_url.rstrip("/") + ("/v1/image_generation" if not parsed.path else "")
        self.key = api_key
        self.model = model
        self.session = session or requests.Session()

    def generate(self, prompt, width, height, output_path, reference_image=None):
        if not prompt.strip() or len(prompt) > 1500:
            raise MiniMaxImageError("MiniMax 图片提示词必须为1至1500字符")
        if self.model != "image-01":
            raise MiniMaxImageError("当前逐镜生图使用 image-01 以固定画幅")
        if any(type(v) is not int or not 512 <= v <= 2048 or v % 8 for v in (width, height)):
            raise MiniMaxImageError("图片尺寸必须为512至2048且为8的倍数")
        body = {"model": self.model, "prompt": prompt, "width": width, "height": height,
                "response_format": "base64", "n": 1, "prompt_optimizer": False}
        if reference_image:
            path = Path(reference_image)
            if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                raise MiniMaxImageError("IP参考图片不存在或超过10MB")
            with Image.open(path) as picture:
                picture.load()
                memory = io.BytesIO()
                picture.convert("RGB").save(memory, format="JPEG", quality=90)
            encoded = base64.b64encode(memory.getvalue()).decode("ascii")
            if len(encoded) > 14 * 1024 * 1024:
                raise MiniMaxImageError("IP参考图片过大")
            body["subject_reference"] = [{"type": "character", "image_file": "data:image/jpeg;base64," + encoded}]
        try:
            response = self.session.post(self.url, headers={"Authorization": "Bearer " + self.key}, json=body, timeout=120)
            if response.status_code != 200:
                raise MiniMaxImageError("MiniMax 生图请求失败，请检查配置或稍后重试")
            data = response.json()
            if (data.get("base_resp") or {}).get("status_code", 0) != 0:
                raise MiniMaxImageError("MiniMax 生图未成功，请检查账号与请求参数")
            values = (data.get("data") or {}).get("image_base64") or []
            if not isinstance(values, list) or len(values) != 1:
                raise MiniMaxImageError("MiniMax 未返回单张图片")
            binary = base64.b64decode(values[0], validate=True)
            with Image.open(io.BytesIO(binary)) as picture:
                picture.load()
                if picture.size != (width, height):
                    raise MiniMaxImageError("MiniMax 返回图片尺寸不符合分镜画幅")
                destination = Path(output_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                picture.convert("RGB").save(destination, format="PNG")
            return str(destination)
        except MiniMaxImageError:
            raise
        except Exception:
            raise MiniMaxImageError("MiniMax 图片响应无法处理") from None
