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
