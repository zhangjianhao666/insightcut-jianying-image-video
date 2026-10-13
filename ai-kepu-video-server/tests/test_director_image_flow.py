import base64
import io
import json

import pytest
from PIL import Image

from src.media.minimax_image import MiniMaxImageError, MiniMaxImageGenerator
from src.text.storyboard_director import direct_segments


class Director:
    def __init__(self, proposals):
        self.proposals = iter(proposals)
        self.calls = 0

    def _call_api(self, **kwargs):
        self.calls += 1
        return json.dumps(next(self.proposals), ensure_ascii=False)


def test_director_can_use_variable_durations_without_rewriting():
    g = Director([{'shots': [
        {'text': '鹰嘴豆是豆类。', 'duration': 8, 'purpose': '特写展示豆形'},
        {'text': '煮熟再食用。', 'duration': 12, 'purpose': '俯拍烹饪示范'},
    ]}])
    result = direct_segments(g, '鹰嘴豆是豆类。\n煮熟再食用。', target_duration=20)
    assert [x['duration'] for x in result] == [8, 12]
    assert g.calls == 1


def test_director_repairs_missing_text_and_wrong_total():
    bad = {'shots': [{'text': '豆子。', 'duration': 5, 'purpose': '特写'}]}
    good = {'shots': [{'text': '鹰嘴豆。', 'duration': 10, 'purpose': '特写'}]}
    g = Director([bad, good])
    assert direct_segments(g, '鹰嘴豆。', target_duration=10)[0]['text'] == '鹰嘴豆。'
    assert g.calls == 2


def test_director_never_falls_back_to_mechanical_chunks():
    g = Director([{'shots': []}, {'shots': []}])
    with pytest.raises(ValueError, match='导演分镜边界规划失败'):
        direct_segments(g, '鹰嘴豆。', target_duration=10)


class Session:
    def post(self, url, **kwargs):
        self.body = kwargs['json']
        self.url = url
        b = io.BytesIO()
        Image.new('RGB', (512, 512)).save(b, format='PNG')
        encoded = base64.b64encode(b.getvalue()).decode()
        class Response:
            status_code = 200
            def json(self):
                return {'base_resp': {'status_code': 0}, 'data': {'image_base64': [encoded]}}
        return Response()


def test_image_binds_local_reference_and_archives_output(tmp_path):
    ref = tmp_path / 'ref.png'
    Image.new('RGB', (64, 64)).save(ref)
    session = Session()
    g = MiniMaxImageGenerator('secret', 'https://api.minimax.cn', session=session)
    out = g.generate('A chickpea closeup', 512, 512, tmp_path / 'out.png', reference_image=ref)
    assert Image.open(out).size == (512, 512)
    assert session.body['subject_reference'][0]['type'] == 'character'
    assert session.body['subject_reference'][0]['image_file'].startswith('data:image/jpeg;base64,')
    assert session.body['prompt_optimizer'] is False
    assert session.body['n'] == 1


@pytest.mark.parametrize('url', ['http://api.minimax.cn', 'https://127.0.0.1', 'https://api.minimax.cn.evil.com'])
def test_image_rejects_untrusted_endpoint(url):
    with pytest.raises(MiniMaxImageError):
        MiniMaxImageGenerator('secret', url)


def test_bound_reference_without_identity_cannot_invent_character(tmp_path, monkeypatch):
    from src.config import Config
    from src.text import ip_library, storyboard_director
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(ip_library, 'project_ip', lambda _: {'name': 'Vivian', 'reference_image': 'ref.png'})
    g = Director([])
    plan = storyboard_director.plan_storyboard('test', g, script='豆子。', segments=['豆子。'],
                                               summary='豆子', style='2D', ratio='16:9')
    assert plan['status'] == 'failed'
    assert plan['error']['category'] == 'reference_identity_missing'
    assert g.calls == 0


def test_boundary_director_chooses_visual_language_without_fixed_examples():
    class Capture(Director):
        def _call_api(self, **kwargs):
            self.prompt = kwargs['messages'][0]['content']
            return super()._call_api(**kwargs)
    g = Capture([{'shots': [{'text': '鹰嘴豆。', 'duration': 10, 'purpose': '介绍豆类'}]}])
    direct_segments(g, '鹰嘴豆。', target_duration=10)
    assert '可用人物中景、食材特写、俯拍示范、无文字示意图' not in g.prompt
    assert '自主' in g.prompt


def test_native_director_can_keep_ip_narration_offscreen(tmp_path, monkeypatch):
    from src.config import Config
    from src.text import ip_library, storyboard_director
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(Config, 'load_model_config', lambda: {'video': {'provider': 'minimax', 'model': 'MiniMax-H3'}})
    monkeypatch.setattr(ip_library, 'project_ip', lambda _: {'name': 'Vivian', 'visual_identity': 'Same Vivian brown hair', 'outfit': 'Burgundy top'})
    class Capture:
        def _call_api(self, **kwargs):
            self.prompt = kwargs['messages'][0]['content']
            return json.dumps({'shots': [], 'visual_bible': {}})
    g = Capture()
    storyboard_director.plan_storyboard('test', g, script='鹰嘴豆。', segments=['鹰嘴豆。'], summary='豆类', style='Family Guy', ratio='16:9')
    assert '每个分镜由同一 IP 主持人说出对应原文' not in g.prompt
    assert '画外' in g.prompt
    assert '衣袖' in g.prompt and 'Burgundy top' in g.prompt


def test_shot_design_purpose_survives_outline_merge(tmp_path, monkeypatch):
    from src.config import Config
    from src.text import ip_library, storyboard_director
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(ip_library, 'project_ip', lambda _: None)
    shot = {'index': 0, 'purpose': '通过碗与餐盘的空间关系表达日常一餐',
            'image_prompt': 'One white bowl and a plain dinner plate on a wooden table',
            'motion_prompt': '开场保持固定画风、桌面、碗和餐盘的位置关系，光线平稳，所有物体边缘清晰。' * 3 + '；碗缓慢移向餐盘，动作平稳不夸张，桌面与背景没有变化；[Static shot] 镜头保持俯拍，不发生跳切，不添加人物或字幕；结尾碗与餐盘并排停稳，留下明确稳定的画面。',
            'camera': 'top-down', 'start_state': 'bowl beside plate', 'end_state': 'bowl close to plate'}
    g = Director([{'visual_bible': {}, 'shots': [shot]}])
    plan = storyboard_director.plan_storyboard('test', g, script='鹰嘴豆。', segments=['鹰嘴豆。'],
        summary='豆类', style='2D', ratio='16:9', target_duration=10,
        outline=[{'text': '鹰嘴豆。', 'duration': 10, 'purpose': '介绍日常食材'}])
    assert plan['status'] == 'completed'
    assert plan['shots']['0']['purpose'] == shot['purpose']
    assert plan['shots']['0']['narrative_purpose'] == '介绍日常食材'


def test_visual_purpose_is_not_silently_truncated():
    from src.text.storyboard_director import _normalize_plan
    purpose = '这一镜完整视觉设计的理由。' * 50
    plan = _normalize_plan({'shots': [{'index': 0, 'image_prompt': 'A white bowl of beige chickpeas', 'purpose': purpose}]}, 1)
    assert plan['shots']['0']['purpose'] == purpose


def test_director_preserves_specific_provider_failure(tmp_path, monkeypatch):
    from src.config import Config
    from src.text import ip_library, storyboard_director
    from src.api.error_model import ClassifiedError, ErrorCode, SafeError
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(ip_library, 'project_ip', lambda _: None)
    class Fail:
        def _call_api(self, **kwargs):
            raise ClassifiedError(SafeError(code=ErrorCode.PROVIDER_ERROR, retryable=False,
                safe_message='模型输出因长度限制被截断，请调整输出预算后重试。', provider='llm'))
    plan = storyboard_director.plan_storyboard('test', Fail(), script='豆子。', segments=['豆子。'],
        summary='豆类', style='2D', ratio='16:9')
    assert plan['status'] == 'failed'
    assert '截断' in plan['error']['safe_message']
    assert plan['error']['retryable'] is False


def test_shot_director_receives_narrative_brief_without_output_purpose_key(tmp_path, monkeypatch):
    from src.config import Config
    from src.text import ip_library, storyboard_director
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(ip_library, 'project_ip', lambda _: None)
    class Capture:
        def _call_api(self, **kwargs):
            self.prompt = kwargs['messages'][0]['content']
            return json.dumps({'shots': []})
    g = Capture()
    storyboard_director.plan_storyboard('test', g, script='豆子。', segments=['豆子。'],
        summary='豆类', style='2D', ratio='16:9', target_duration=10,
        outline=[{'text': '豆子。', 'duration': 10, 'purpose': '介绍豆类身份'}])
    assert '"narrative_purpose": "介绍豆类身份"' in g.prompt
    assert '"purpose": "介绍豆类身份"' not in g.prompt
    assert '"duration": 10' in g.prompt and '"text": "豆子。"' in g.prompt


@pytest.mark.parametrize('provider,model,expected', [
    ('deepseek', 'deepseek/deepseek-v4-pro', 64000),
    ('deepseek', 'deepseek-v4-pro', 64000),
    ('deepseek', 'deepseek/deepseek-chat', 16000),
    ('custom', 'deepseek-v4-pro', 16000),
])
def test_boundary_director_budget_includes_pro_reasoning(provider, model, expected):
    class Capture(Director):
        def _call_api(self, **kwargs):
            self.budget = kwargs['max_tokens']
            return super()._call_api(**kwargs)
    g = Capture([{'shots': [{'text': '豆子。', 'duration': 10, 'purpose': '介绍豆类'}]}])
    g.provider, g.model = provider, model
    direct_segments(g, '豆子。', target_duration=10)
    assert g.budget == expected


@pytest.mark.parametrize('provider,model,expected', [
    ('deepseek', 'deepseek/deepseek-v4-pro', 64000),
    ('deepseek', 'deepseek/deepseek-chat', 22000),
    ('custom', 'deepseek-v4-pro', 22000),
])
def test_ten_shot_director_budget_includes_pro_reasoning(tmp_path, monkeypatch, provider, model, expected):
    from src.config import Config
    from src.text import ip_library, storyboard_director
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(ip_library, 'project_ip', lambda _: None)
    class Capture:
        def _call_api(self, **kwargs):
            self.budget = kwargs['max_tokens']
            return json.dumps({'shots': []})
    g = Capture()
    g.provider, g.model = provider, model
    storyboard_director.plan_storyboard('test', g, script='豆子。' * 10, segments=['豆子。'] * 10,
        summary='豆类', style='2D', ratio='16:9', target_duration=120)
    assert g.budget == expected
