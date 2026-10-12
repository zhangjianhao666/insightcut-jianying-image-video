"""Compile authoritative segment dialogue, independently of LLM paraphrases."""
import re

from src.media.minimax_i2v import is_h3_model
from src.text.ip_library import project_ip


def compile_native_dialogue(prompt, text, *, profile=None, has_voice_reference=False, limit=1800):
    script = str(text or "").strip()
    if not script:
        return str(prompt or "")[:limit]
    if re.search(r'</?d\b', script, re.I):
        raise ValueError("台词不能包含对白控制标签，请使用普通文本")
    profile = profile or {}
    name = str(profile.get("name") or "the recurring host")[:80]
    style = str(profile.get("voice_style") or "warm, clear Mandarin, natural explanatory delivery")[:250]
    language = "Chinese" if re.search(r'[\u4e00-\u9fff]', script) else "English"
    reference = (
        "<Audio 1> is the voice-timbre reference for this speaker (S1). "
        "Preserve its vocal identity; do not copy its old words, music or background noise. "
        if has_voice_reference else "Keep the same speaker's vocal identity and delivery throughout. "
    )
    speech = (
        f"Native spoken dialogue is required. {name} (S1), voice style: {style}. "
        f"{reference}Say exactly once, without paraphrasing: <d>[{language}] {script}</d> "
        "Synchronize the speaking mouth with this dialogue; finish the complete line before the clip ends. "
        "No extra spoken words. Dialogue is foreground, effects are quieter, no generated captions. "
        "These speech instructions override older no-speech or no-mouth-movement directions. "
        "Visual action: "
    )
    if len(speech) > limit:
        raise ValueError("台词过长，无法完整放入视频提示词；请拆分该分镜后再生成")
    visual = str(prompt or "").replace("或口型说话", "").replace("不含口播台词", "")
    visual = re.sub(r'\bno (?:speech|dialogue|voiceover|narration)\b', '', visual, flags=re.I)
    budget = limit - len(speech)
    marker = "动作与运镜："
    if marker in visual and len(visual) > budget and budget > len(marker):
        constraints, actions = visual.split(marker, 1)
        action_budget = min(len(actions), max(250, budget // 2), budget - len(marker))
        visual = constraints[:budget - len(marker) - action_budget] + marker + actions[:action_budget]
    return speech + visual[:budget]


def compile_video_prompt(task_id, text, prompt, references, video_config):
    if video_config.get("provider") != "minimax" or not is_h3_model(video_config.get("model")):
        return prompt
    return compile_native_dialogue(
        prompt, text, profile=project_ip(task_id),
        has_voice_reference=bool((references or {}).get("audio")),
    )
