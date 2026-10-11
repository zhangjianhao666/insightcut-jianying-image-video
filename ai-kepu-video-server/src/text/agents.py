"""
文本 Agent 模块
提供文案改写和逐段图片 prompt 生成能力。
"""

import json
import logging
import re
from typing import Optional

from .generator import ArticleGenerator

logger = logging.getLogger(__name__)


class ScriptRewriter:
    """文案改写 Agent - 将用户原文改写为旁白脚本，并生成总结"""

    def __init__(self, article_generator: Optional[ArticleGenerator] = None):
        self.article_generator = article_generator or ArticleGenerator()

    def rewrite(self, raw_text: str, style: str, target_length: int = None, input_mode: str = None) -> dict:
        """
        输入用户原始文案，输出改写后的旁白脚本和 50-100 字内容总结。

        input_mode=theme 时按主题生成；input_mode=script 时按用户文案改写。
        未传 input_mode 时保留兼容逻辑：raw_text <= 100 字视为主题。
        """
        raw_text = (raw_text or "").strip()
        if not raw_text:
            raise ValueError("文案不能为空")

        if input_mode == "theme" or (input_mode is None and len(raw_text) <= 100):
            prompt = self._theme_prompt(raw_text, style, target_length)
        else:
            prompt = self._rewrite_prompt(raw_text, style)

        text = self.article_generator._call_api(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
        )
        result = self._parse_json(text)
        script = (result.get("script") or "").strip()
        summary = (result.get("summary") or "").strip()

        if not script:
            raise RuntimeError(f"文案改写 Agent 返回缺少 script: {text[:200]}")
        if not summary:
            summary = self._fallback_summary(script)

        logger.info(f"文案改写完成: script={len(script)} 字, summary={len(summary)} 字")
        return {"script": script, "summary": summary}

    def _theme_prompt(self, theme: str, style: str, target_length: int) -> str:
        length_line = "目标长度：按内容自然生成，不强制字数" if not target_length else f"目标长度：约 {target_length} 字"
        return f"""你是一位专业短视频旁白编剧。

请根据主题生成一篇适合配音朗读的中文短视频旁白脚本，并给出内容总结。

要求：
- 主题：{theme}
- 风格：{style}
- {length_line}
- 语言口语化，适合 TTS 配音
- 每个自然段 1-3 句话
- 不要标题、不要说明文字
- summary 为 50-100 字，概括全文核心内容

只输出 JSON，格式如下：
{{"script":"改写后的旁白脚本","summary":"50-100字内容总结"}}"""

    def _rewrite_prompt(self, raw_text: str, style: str) -> str:
        return f"""你是一位专业短视频旁白改写编剧。

请将用户提供的中文文案改写成适合视频解说和 TTS 配音的旁白脚本，并给出内容总结。

要求：
- 保持原文核心事实、观点和顺序，不要大幅改写含义
- 可以优化口语表达、节奏和衔接，让它更适合解说
- 风格：{style}
- 每个自然段 1-3 句话
- 不要标题、不要说明文字
- summary 为 50-100 字，概括全文核心内容

用户原文：
{raw_text}

只输出 JSON，格式如下：
{{"script":"改写后的旁白脚本","summary":"50-100字内容总结"}}"""

    def _parse_json(self, text: str) -> dict:
        text = (text or "").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except json.JSONDecodeError:
                pass

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

        raise RuntimeError(f"无法解析 Agent JSON 输出: {text[:200]}")

    def _fallback_summary(self, script: str) -> str:
        compact = re.sub(r"\s+", "", script)
        return compact[:100]


class ImagePromptAgent:
    """图片 Prompt Agent - 逐段生成图片描述"""

    def __init__(self, article_generator: Optional[ArticleGenerator] = None):
        self.article_generator = article_generator or ArticleGenerator()

    def generate_prompt(
        self, segment_text: str, summary: str, style: str,
        aspect_ratio: str = None, visual_bible: dict = None,
        previous_segment: str = "", next_segment: str = "",
    ) -> str:
        """Use full-video art direction and narrative neighbors as context."""
        bible_text = json.dumps(visual_bible or {}, ensure_ascii=False)[:1100]
        user_prompt = f"""You are the cinematographer of one continuous educational video.
Create one English image-generation prompt for the CURRENT narration shot.

Full video summary (story context, not all to be drawn):
{summary[:550]}

Shared visual bible (MUST preserve identities, costumes, colors, sets):
{bible_text}

Previous narration: {previous_segment[:160]}
CURRENT narration: {segment_text[:300]}
Next narration: {next_segment[:160]}

Visual style: {style[:250]}
Aspect ratio: {aspect_ratio or "16:9"}

Requirements:
- Describe actual visible subjects, environment, shot size, composition and lighting.
- Use 35-75 English words, one coherent shot, no explanatory commentary.
- Use the SAME recurring character descriptions and costume across scenes.
- Keep the shot distinct from adjacent shots; vary wide/medium/close framing.
- No overlay text, typography, captions, logos, watermarks or pseudo-writing.
- Maintain continuity with previous and following shots.
Return only the English visual prompt."""

        result = self.article_generator._call_api(
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=320,
        )
        prompt = self._clean_prompt(result)
        logger.info("当前分镜画面提示词规划完成")
        return prompt

    def _clean_prompt(self, text: str) -> str:
        prompt = (text or "").strip()
        prompt = re.sub(r"^```(?:text)?\s*|\s*```$", "", prompt, flags=re.S).strip()
        prompt = prompt.splitlines()[0].strip() if "\n" in prompt else prompt
        prompt = re.sub(r"^\s*[-*\d.)]+\s*", "", prompt).strip()
        prompt = prompt.strip("\"'“”‘’")
        words = prompt.split()
        if len(words) > 85:
            prompt = " ".join(words[:85])
        if not prompt:
            prompt = "cinematic educational scene, clear subject, natural lighting, detailed composition"
        return prompt
