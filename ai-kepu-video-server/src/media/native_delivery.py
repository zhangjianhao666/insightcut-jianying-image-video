"""Local, resumable delivery of native dialogue. Never calls a media provider."""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from src.draft.subtitle import SubtitleWriter
from src.utils.rendering import normalize_ratio


class DeliveryError(ValueError):
    """Safe operator-facing blocker; originals remain available."""


def _run(command, *, timeout=900, cwd=None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                cwd=cwd, encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        raise DeliveryError('本地音轨处理无法完成，请检查运行环境后重试；原始视频已保留') from None
    if result.returncode:
        raise DeliveryError('本地音轨处理失败，请检查模型或视频文件；原始视频已保留')
    return result.stdout


def _ffmpeg():
    candidates = [os.getenv('NATIVE_DELIVERY_FFMPEG'), shutil.which('ffmpeg')]
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError):
        pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            filters = _run([candidate, '-hide_banner', '-filters'], timeout=30)
            if ' subtitles ' in filters:
                return str(candidate)
    raise DeliveryError('FFmpeg 缺少 subtitles/libass 滤镜，请配置 NATIVE_DELIVERY_FFMPEG')


def preflight():
    """Check before paid generation; never fetch an ASR model automatically."""
    python = os.getenv('NATIVE_ASR_PYTHON', '')
    model = Path(os.getenv('NATIVE_ASR_MODEL_PATH', '/nonexistent'))
    if not python or not Path(python).is_file() or not all(
        (model / name).is_file() for name in ('model.bin', 'config.json', 'tokenizer.json')
    ):
        raise DeliveryError('请配置本地语音识别 NATIVE_ASR_PYTHON 和 NATIVE_ASR_MODEL_PATH；不会自动下载模型')
    _run([python, '-c', 'import faster_whisper'], timeout=30)
    if not shutil.which('ffprobe'):
        raise DeliveryError('本地交付需要 FFprobe')
    return {'python': python, 'model': str(model.resolve()), 'ffmpeg': _ffmpeg(),
            'model_identity': _digest(model/'model.bin')+_digest(model/'config.json')+_digest(model/'tokenizer.json')}


def probe(path):
    binary = shutil.which('ffprobe')
    if not binary:
        raise DeliveryError('本地交付需要 FFprobe')
    try:
        data = json.loads(_run([binary, '-v', 'error', '-show_entries',
                               'format=duration:stream=codec_type,codec_name', '-of', 'json', str(path)], timeout=30))
        duration = float(data['format']['duration'])
        kinds = {s['codec_type'] for s in data.get('streams', [])}
        if not math.isfinite(duration) or duration <= .1 or 'video' not in kinds:
            raise ValueError()
        return duration, kinds
    except (ValueError, KeyError, TypeError):
        raise DeliveryError('动态视频轨道或时长无效，原始视频已保留') from None


def _normalized(text):
    return ''.join(c.lower() for c in str(text) if c.isalnum())


def validate_dialogue(text, speech):
    expected = _normalized(text)
    actual = _normalized(''.join(str(s.get('text') or '') for s in speech))
    if not actual:
        raise DeliveryError('未识别到原生对白，请复核镜头；不会以字幕冒充已说出的台词')
    matcher = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
    mismatch = not expected or matcher.ratio() < (.66 if len(expected) <= 3 else .72)
    # An entire omitted/extra phrase must not hide inside a high whole-text ratio.
    for operation, left, right, begin, end in matcher.get_opcodes():
        if operation == "delete" and right-left >= 2 or operation == "insert" and end-begin >= 2:
            mismatch = True
    blocks = matcher.get_matching_blocks()
    for original, axis in ((text, 0), (' '.join(str(s.get('text') or '') for s in speech), 1)):
        cursor = 0
        for clause in re.split(r'[。！？；，,!?;.]', str(original)):
            count = len(_normalized(clause))
            if not count:
                continue
            coverage = sum(max(0, min(cursor+count, block[axis]+block.size)-max(cursor, block[axis])) for block in blocks)
            if coverage < max(1, count-math.ceil(count*.28)):
                mismatch = True
            cursor += count
    if mismatch:
        raise DeliveryError('原生对白与文稿差异较大，需要人工复核或缩短文稿；不会自动重生成付费视频')


def delivery_windows(durations, speeches, max_duration):
    if len(durations) != len(speeches) or not durations:
        raise DeliveryError('镜头与语音时间轴不完整')
    if max_duration is not None and (not math.isfinite(float(max_duration)) or float(max_duration) <= 0):
        raise DeliveryError('总时长必须是正数')
    windows = []
    for duration, speech in zip(durations, speeches):
        if not math.isfinite(duration) or not speech:
            raise DeliveryError('缺少有效对白时间轴')
        previous_end = 0
        for item in speech:
            start, end = float(item['start']), float(item['end'])
            if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= duration + .1 or start < previous_end - .1:
                raise DeliveryError('对白时间轴超出原始视频范围，请复核')
            previous_end = end
        start = max(0., float(speech[0]['start']) - .35) if max_duration else 0.
        end = min(duration, float(speech[-1]['end']) + .25) if max_duration else duration
        windows.append({'start': start, 'end': end})
    # Reserve a frame + AAC encoder delay per cut. Never remove speech to fit.
    conservative_duration = sum(math.ceil((w['end']-w['start'])*30)/30 + .04 for w in windows)
    if max_duration is not None and conservative_duration > float(max_duration):
        raise DeliveryError('完整对白超过总时长预算，请缩短文稿或提高总时长；原始视频已保留')
    return windows


def write_subtitles(directory, texts, speeches, windows, rendered_durations, *, max_line_width=40):
    from src.utils.subtitle_text import (
        normalize_subtitle_text, subtitle_display_width, wrap_subtitle_text,
    )
    spans = []
    def append(text, length):
        if length > .00001:
            spans.append({'segment_index': len(spans), 'text': text, 'duration': length})
    for original, speech, window, duration in zip(texts, speeches, windows, rendered_durations):
        cursor = 0.
        for index, item in enumerate(speech):
            start = max(cursor, float(item['start']) - window['start'], 0.)
            end = min(duration, float(item['end']) - window['start'] + (.04 if index == len(speech)-1 else 0))
            text = normalize_subtitle_text(item.get('text') or (original if len(speech) == 1 else ''))
            if end <= start or not text:
                continue
            lines = wrap_subtitle_text(text, max_line_width)
            # At most two lines per cue. Sentence timestamps are measured;
            # sub-cue timing within a long sentence is proportionally estimated.
            chunks = [''.join(lines[i:i+2]) for i in range(0, len(lines), 2)]
            weights = [subtitle_display_width(chunk) for chunk in chunks]
            append('', start-cursor)
            for chunk, weight in zip(chunks, weights):
                append(chunk, (end-start)*weight/sum(weights))
            cursor = end
        append('', duration-cursor)
    results = []
    for format in ('srt', 'vtt'):
        raw = SubtitleWriter(max_line_width=max_line_width).render(spans, format)
        if format == 'vtt':
            raw = raw.removeprefix('WEBVTT\n\n')
        blocks = []
        for block in raw.split('\n\n'):
            lines = block.strip().splitlines()
            if len(lines) >= 3:
                blocks.append(str(len(blocks)+1)+'\n'+'\n'.join(lines[1:]))
        path = Path(directory) / f'dialogue.{format}'
        path.write_text(('WEBVTT\n\n' if format == 'vtt' else '')+'\n\n'.join(blocks)+'\n', encoding='utf-8')
        results.append(path)
    return tuple(results)


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def transcribe(path, cache_dir, runtime):
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((_digest(path)+runtime['model']+runtime.get('model_identity', '')+'whisper-zh-vad-beam5-v2').encode()).hexdigest()
    report = cache_dir / f'{key}.json'
    if report.is_file():
        try:
            return json.loads(report.read_text())['speech']
        except (ValueError, KeyError, TypeError):
            pass
    wave = cache_dir / f'{key}.wav'
    _run([runtime['ffmpeg'], '-v', 'error', '-y', '-i', str(path), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wave)])
    script = Path(__file__).resolve().parents[2] / 'scripts/transcribe_native.py'
    staged = report.with_suffix('.pending.json')
    _run([runtime['python'], str(script), '--model', runtime['model'], '--input', str(wave), '--output', str(staged)], timeout=300)
    try:
        speech = json.loads(staged.read_text())['speech']
        if not isinstance(speech, list):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise DeliveryError('本地识别没有有效时间轴，原始视频已保留') from None
    os.replace(staged, report)
    return speech


def deliver(paths, shots, output, *, ratio='16:9', max_duration=None, subtitles=True,
            stage_callback=None, recognizer=None, runtime=None, asset_callback=None):
    """All derivatives are versioned. Only a verified final replaces output."""
    runtime = runtime or preflight()
    if len(paths) != len(shots) or not paths or any(not Path(p).is_file() for p in paths):
        raise DeliveryError('原始动态视频片段缺失，禁止用图片替代')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    run = output.parent / 'deliveries' / uuid.uuid4().hex
    run.mkdir(parents=True)
    def stage(value):
        if stage_callback:
            stage_callback(value)
    stage('transcribing')
    durations, speeches = [], []
    for path, shot in zip(paths, shots):
        duration, kinds = probe(path)
        if 'audio' not in kinds:
            raise DeliveryError('原生对白镜头没有音轨，无法自动交付；原始片段已保留')
        speech = (recognizer(path) if recognizer else transcribe(path, output.parent/'transcripts', runtime))
        validate_dialogue(shot.get('text'), speech)
        durations.append(duration)
        speeches.append(speech)
    windows = delivery_windows(durations, speeches, max_duration)
    (run/'timeline.json').write_text(json.dumps({'speech': speeches, 'windows': windows}, ensure_ascii=False, indent=2))
    stage('rendering')
    ff = runtime['ffmpeg']
    ratio = normalize_ratio(ratio)
    width, height = {'16:9':(1280,720), '9:16':(720,1280), '3:4':(720,960)}[ratio]
    rendered = []
    for i, (path, window) in enumerate(zip(paths, windows)):
        target = run/f'clip_{i:04d}.mp4'
        filters = f'fps=30,scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,setpts=PTS-STARTPTS'
        _run([ff,'-v','error','-y','-ss',str(window['start']),'-i',str(path),'-t',str(window['end']-window['start']),
              '-map','0:v:0','-map','0:a:0','-vf',filters,'-c:v','libx264','-preset','veryfast','-crf','20',
              '-af','loudnorm=I=-16:TP=-1.5:LRA=7,aresample=48000','-c:a','aac','-b:a','160k','-ac','2',str(target)])
        rendered.append(probe(target)[0])
    manifest = run/'concat.txt'
    manifest.write_text(''.join(f"file 'clip_{i:04d}.mp4'\n" for i in range(len(paths))))
    assembled = run/'native_assembled.mp4'
    _run([ff,'-v','error','-y','-f','concat','-safe','0','-i','concat.txt','-map','0:v:0','-map','0:a:0','-c','copy','-movflags','+faststart',str(assembled)], cwd=run)
    if asset_callback:
        asset_callback({'delivery_path':str(assembled)})
    stage('subtitling')
    srt, vtt = write_subtitles(run, [s['text'] for s in shots], speeches, windows, rendered,
                               max_line_width=40 if ratio == '16:9' else 18)
    if asset_callback:
        asset_callback({'subtitle_path':str(srt), 'vtt_path':str(vtt)})
    final = run/'captioned.mp4'
    if subtitles:
        font = 'Heiti SC' if Path('/System/Library/Fonts').is_dir() else 'Noto Sans CJK SC'
        font_size = 18 if ratio == '16:9' else 14
        style = f'FontName={font},FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=1.4,Shadow=0.5,Alignment=2,MarginV=18,MarginL=24,MarginR=24'
        _run([ff,'-v','error','-y','-i',str(assembled),'-vf',f"subtitles=filename=dialogue.srt:force_style='{style}'",'-c:v','libx264','-preset','veryfast','-crf','20','-c:a','copy','-movflags','+faststart',str(final)],cwd=run)
    else:
        shutil.copy2(assembled, final)
    duration, kinds = probe(final)
    if kinds != {'video','audio'} or (max_duration is not None and duration > float(max_duration)):
        raise DeliveryError('成片音轨或总时长验收失败，衍生文件与原始片段已保留')
    _run([ff,'-v','error','-i',str(final),'-f','null','-'])
    stage('publishing')
    if output.is_file():
        shutil.copy2(output, run/'previous_published.mp4')
    # Asset history refers to this immutable final. Existing published output survives failures.
    staged = output.with_name(f'.{output.stem}.{uuid.uuid4().hex}.mp4')
    shutil.copy2(final, staged)
    os.replace(staged, output)
    return {'delivery_duration':duration, 'subtitle_path':str(srt), 'vtt_path':str(vtt),
            'delivery_path':str(final), 'trim_windows':windows, 'has_audio':True,
            'dialogue_verified':True, 'dialogue_check':'local_asr_with_phrase_coverage'}
