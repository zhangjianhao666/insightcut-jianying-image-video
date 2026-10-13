"""字幕文本清洗规则。"""

import re


_SUBTITLE_PUNCT = re.compile(r'[。！？!?…，,；;、：:]')
_WHITESPACE = re.compile(r"\s+")


def normalize_subtitle_text(text: str) -> str:
    """字幕保持单行，去掉口播停顿标点并保留引号、书名号等语义符号。"""
    raw = str(text or "")
    compact = _WHITESPACE.sub(" ", raw).strip()
    clean = _SUBTITLE_PUNCT.sub("", compact).strip()
    return clean or compact


def subtitle_display_width(text: str) -> int:
    """Count wide CJK glyphs as two columns and combining marks as zero."""
    import unicodedata
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
               for char in text)


def wrap_subtitle_text(text: str, max_width: int) -> list[str]:
    """Wrap normalized captions, keeping short Latin words together."""
    def wrap_at(limit):
        lines, current, width = [], '', 0
        for word in re.findall(r'[A-Za-z0-9]+|.', text):
            tokens = [word] if subtitle_display_width(word) <= limit else list(word)
            for token in tokens:
                size = subtitle_display_width(token)
                if current and width + size > limit:
                    lines.append(current)
                    current, width = '', 0
                current += token
                width += size
        if current:
            lines.append(current)
        return lines
    lines = wrap_at(max_width)
    if len(lines) == 2:
        target = (subtitle_display_width(text) + 1) // 2
        if all(len(word) <= target for word in re.findall(r'[A-Za-z0-9]+', text)):
            balanced = wrap_at(target)
            if len(balanced) == 2:
                lines = balanced
    return lines
