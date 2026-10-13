"""Compile one shot's actual visual intent without portrait-biased boilerplate."""
import ast
import hashlib
import json
import re

CONTRACT_VERSION = 1
MODES = {'character', 'hands', 'object', 'diagram'}
FRAMINGS = {'wide', 'medium', 'close_up', 'extreme_close_up', 'top_down', 'unspecified'}
MARKER = '\nVISUAL CONTRACT:'


def _structured(value, warnings):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith(('{', '[')):
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except (ValueError, SyntaxError, TypeError):
                pass
        warnings.append('legacy_truncated_constraint')
        return ''
    if re.search(r'\b(?:no|and|or|with|a|the)\s*$', text, re.I):
        warnings.append('legacy_truncated_constraint')
        return ''
    return text


def _render(value):
    if isinstance(value, dict):
        return '; '.join(f'{key}: {_render(item)}' for key, item in value.items() if item)
    if isinstance(value, list):
        return '; '.join(_render(item) for item in value if item)
    return str(value or '').strip()


def _environment(value, shot):
    if not isinstance(value, dict):
        return _render(value)
    scene, location = shot.get('scene_id'), shot.get('location_id')
    selected = []
    if scene and value.get(scene):
        selected.append(value[scene])
    for key in ['scenes', 'locations']:
        entries = value.get(key)
        if isinstance(entries, dict):
            for identity in [scene, location]:
                if identity and entries.get(identity):
                    selected.append(entries[identity])
    return _render(selected if selected else value)


def _infer_mode(prompt, shot, bible):
    if shot.get('shot_type') == 'diagram':
        return 'diagram'
    # A possessive name followed by hands does not require the person's face.
    without_hands = re.sub(r"\b[A-Za-z]+['’]s\s+(?:two\s+)?hands?\b", '', prompt, flags=re.I)
    # Negative exclusions describe absence, not the shot's visible subject.
    without_hands = re.sub(r'\b(?:no|without|exclude|excluding)\b[^.;\n]*', '', without_hands, flags=re.I)
    visible_person = re.search(r'\b(?:woman|man|person|host|character|face|portrait|vivian)\b', without_hands, re.I)
    cast = shot.get('character_ids') or []
    if any(isinstance(name, str) and re.search(r'\b'+re.escape(name)+r'\b', without_hands, re.I) for name in cast):
        visible_person = True
    if visible_person:
        return 'character'
    if re.search(r'\bhands?\b', prompt, re.I):
        return 'hands'
    if not shot and bible.get('character_lock'):
        return 'character'  # Compatibility for non-director, legacy pipelines.
    return 'object'


def _framing(camera):
    text = str(camera or '').lower()
    for pattern, value in [(r'top.down|overhead|俯拍', 'top_down'),
                           (r'extreme.close|macro|极近|特写特', 'extreme_close_up'),
                           (r'close.up|特写', 'close_up'), (r'medium|中景', 'medium'),
                           (r'wide|full.body|全景', 'wide')]:
        if re.search(pattern, text):
            return value
    return 'unspecified'


def compile_shot(prompt, bible, shot=None, *, max_length=None):
    shot, bible = shot or {}, bible or {}
    warnings = []
    # Strip previously assembled locks so provider switches recompile complete
    # current constraints, instead of carrying stale fragments into new requests.
    text = str(prompt or '').split(MARKER, 1)[0].split('. PROJECT-WIDE VISUAL CONTINUITY:', 1)[0].strip()
    legacy_prefix = '以输入参考图中的同一角色为固定系列IP：锁定脸型、毛色、服饰、人物比例与主要视觉特征。改变场景和动作，但不要改变IP身份。'
    if text.startswith(legacy_prefix):
        text = text[len(legacy_prefix):].strip()
    if not text:
        raise ValueError('分镜图片描述为空')
    declared = shot.get('visual_contract') or {}
    if not isinstance(declared, dict):
        raise ValueError('导演画面契约格式无效')
    mode = declared.get('subject_mode') or _infer_mode(text, shot, bible)
    framing = declared.get('framing') or _framing(shot.get('camera'))
    policy = declared.get('text_policy', 'forbidden')
    if mode not in MODES or framing not in FRAMINGS or policy != 'forbidden':
        raise ValueError('分镜主体、景别或文字策略不受支持')
    if shot and not declared:
        warnings.append('legacy_contract_inferred')
    character = mode == 'character'
    scene = bool(shot.get('scene_id')) and framing in {'wide', 'medium', 'unspecified'} and mode == 'character'
    parts = [f'Subject mode: {mode}. Framing: {framing}.']
    if mode in {'object', 'diagram'}:
        parts.append('Show only the described objects or diagram. Do not add a presenter, face or full human body.')
        if mode == 'diagram' and re.search(r'\bhands?\b', text, re.I):
            parts.append('Only the explicitly requested hands may appear; do not show a face or torso.')
    elif mode == 'hands':
        parts.append('Show only the requested hands and objects. No face, torso or full human body.')
    locks = [('Identity and outfit', 'character_lock')] if character else []
    locks += [('Relevant environment', 'environment_lock'), ('Art direction', 'art_direction'), ('Avoid', 'negative_rules')]
    for label, key in locks:
        value = _structured(bible.get(key, ''), warnings)
        if key == 'negative_rules' and not character:
            value = '; '.join(clause.strip() for clause in _render(value).split(';')
                              if not re.search(r'vivian|outfit|hairstyle|clothing|shirt|apron|costume|服装|角色外观', clause, re.I))
        rendered = _environment(value, shot) if key == 'environment_lock' else _render(value)
        if rendered:
            parts.append(f'{label}: {rendered}')
    parts.append('No letters, numbers, captions, labels, logos or pseudo-writing. Any readable labels are added in post-production, never painted into this image.')
    compiled = text + MARKER + '\n' + '\n'.join(parts)
    if max_length is not None and len(compiled) > max_length:
        raise ValueError('完整分镜约束超过服务商提示词长度上限，请由导演精简；不得截断规则')
    result = {'contract_version': CONTRACT_VERSION, 'prompt': compiled,
              'subject_mode': mode, 'framing': framing, 'text_policy': policy,
              'character_required': character, 'reference_policy': {'character': character, 'scene': scene},
              'scene_id': shot.get('scene_id') or '', 'warnings': sorted(set(warnings))}
    result['fingerprint'] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return result
