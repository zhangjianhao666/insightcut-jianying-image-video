"""Workflow checks use deterministic model doubles, never the live database."""
import json
from copy import deepcopy

import pytest
from src.config import Config
from src.text import ip_library, storyboard_director as director


def shot(index):
    return {'index':index,'duration':5,'image_prompt':'A blue ball on a wooden table, fixed side view',
            'motion_prompt':'桌上蓝球静止，桌面和光线固定；蓝球缓慢滚动到右侧杯子旁；[Static shot] 保持侧面机位；蓝球停在杯子旁，构图稳定。',
            'camera':'side view','start_state':'ball left of cup','end_state':'ball beside cup',
            'purpose':'用球的位置变化展示前进','scene_id':'table','location_id':'room','character_ids':[],
            'transition_to_next':'cut','visual_contract':{'subject_mode':'object','framing':'medium','text_policy':'forbidden'}}


PASS={'version':2,'status':'passed','issues':[]}
REJECT={'version':2,'status':'needs_revision','issues':[{'index':0,'severity':'blocking','category':'state','code':'state_mismatch','evidence':'杯子位置矛盾','required_change':'统一杯子位置'}]}


@pytest.fixture
def isolated(monkeypatch,tmp_path):
    monkeypatch.setattr(Config,'BASE_DIR',tmp_path)
    monkeypatch.setattr(ip_library,'project_ip',lambda _:None)
    return tmp_path


def arrange(monkeypatch,reviews,patch=None):
    calls=[];queue=iter(reviews)
    def review(*args,**kwargs):
        calls.append('review');value=next(queue)
        if isinstance(value,Exception):raise value
        return deepcopy(value)
    def repair(*args,**kwargs):
        calls.append('repair');return deepcopy(patch or {'shots':[shot(0)]})
    from src.text import storyboard_quality
    monkeypatch.setattr(storyboard_quality, 'review_plan', review)
    monkeypatch.setattr(storyboard_quality, 'repair_shots', repair)
    class Generator:
        provider='deepseek';model='deepseek/deepseek-v4-pro'
        def _call_api(self,**kwargs):
            calls.append('create')
            return json.dumps({'visual_bible':{'art_direction':'flat blue'},'shots':[shot(0),shot(1)]})
    return Generator(),calls


def create(g,**kwargs):
    return director.plan_storyboard('quality',g,script='第一句。第二句。',segments=['第一句。','第二句。'],
      summary='移动',style='2D',ratio='16:9',outline=[{'text':'第一句。','duration':5,'purpose':'开场'},{'text':'第二句。','duration':5,'purpose':'结尾'}],target_duration=10,review_required=True,**kwargs)


def test_reviewed_candidate_passes_and_reuses_verified_cache(isolated,monkeypatch):
    g,calls=arrange(monkeypatch,[PASS])
    p=create(g)
    assert p['status']=='completed'
    assert p['quality']['status']=='passed'
    assert not director.planning_blocked(p)
    assert len(p['shots'])==2
    assert create(g)==p
    assert calls==['create','review']


def test_local_repair_preserves_other_shots_and_rechecks(isolated,monkeypatch):
    patched=shot(0);patched['image_prompt']='A blue ball beside the corrected white cup on the same table'
    g,calls=arrange(monkeypatch,[REJECT,PASS],{'shots':[patched]})
    p=create(g)
    assert p['status']=='completed' and p['quality']['repair_rounds']==1
    assert p['shots']['0']['image_prompt']==patched['image_prompt']
    assert p['shots']['1']['image_prompt']==shot(1)['image_prompt']
    assert p['segments']==['第一句。','第二句。']
    assert calls==['create','review','repair','review']


def test_second_rejection_stops_without_erasing_candidate(isolated,monkeypatch):
    g,calls=arrange(monkeypatch,[REJECT,REJECT])
    p=create(g)
    assert p['status']=='failed' and len(p['shots'])==2
    assert p['quality']['status']=='needs_revision'
    assert director.lookup_shot('quality',0)=={}
    assert calls==['create','review','repair','review']


def test_review_provider_failure_keeps_candidate_and_safe_error(isolated,monkeypatch):
    g,calls=arrange(monkeypatch,[RuntimeError('sk-do-not-persist')])
    p=create(g)
    assert p['status']=='failed' and len(p['shots'])==2
    assert p['quality']['status']=='failed'
    assert 'sk-do-not-persist' not in json.dumps(p)
    assert calls==['create','review']


def test_invalid_patch_never_replaces_original(isolated,monkeypatch):
    bad=shot(0);bad['duration']=10
    g,calls=arrange(monkeypatch,[REJECT],{'shots':[bad]})
    p=create(g)
    assert p['status']=='failed'
    assert p['shots']['0']['duration']==5
    assert p['shots']['0']['image_prompt']==shot(0)['image_prompt']
    assert calls==['create','review','repair']


def test_global_blocker_does_not_expand_to_whole_plan_rewrite(isolated,monkeypatch):
    global_issue=deepcopy(REJECT);global_issue['issues'][0]['index']=None
    g,calls=arrange(monkeypatch,[global_issue])
    p=create(g)
    assert p['status']=='failed' and len(p['shots'])==2
    assert calls==['create','review']


def test_changed_shot_invalidates_review_and_lookup(isolated,monkeypatch):
    g,_=arrange(monkeypatch,[PASS]);p=create(g)
    p['shots']['0']['image_prompt']='A different red box instead of the blue ball'
    director._write('quality',p)
    assert director.planning_blocked(p)
    assert director.lookup_shot('quality',0)=={}


def test_structural_failure_preserves_candidate(isolated,monkeypatch):
    g,calls=arrange(monkeypatch,[])
    original=g._call_api
    def missing_camera(**kwargs):
        data=json.loads(original(**kwargs));data['shots'][0]['camera']='';return json.dumps(data)
    g._call_api=missing_camera
    p=create(g)
    assert p['status']=='failed' and len(p['shots'])==2
    assert calls==['create']


def test_explicit_review_existing_candidate_never_recreates_story(isolated,monkeypatch):
    g,calls=arrange(monkeypatch,[REJECT,REJECT]);p=create(g)
    g,calls=arrange(monkeypatch,[PASS])
    p=director.review_existing_plan('quality',g)
    assert p['status']=='completed'
    assert calls==['review']


def test_existing_review_keeps_original_generation_brief(isolated,monkeypatch):
    g,_=arrange(monkeypatch,[PASS])
    create(g,brief='讲述者保持画外讲述')
    from src.text import storyboard_quality
    def inspect(generator,plan,**kwargs):
        assert plan['review_context']['brief']=='讲述者保持画外讲述'
        return deepcopy(PASS)
    monkeypatch.setattr(storyboard_quality,'review_plan',inspect)
    assert director.review_existing_plan('quality',g)['status']=='completed'


def test_explicit_invalid_review_persists_blocked_candidate(isolated,monkeypatch):
    g,_=arrange(monkeypatch,[PASS]);p=create(g)
    p['review_required']=False
    p['shots']['0']['camera']=''
    director._write('quality',p)
    result=director.review_existing_plan('quality',g)
    assert result['status']=='failed'
    assert result['review_required'] is True and len(result['shots'])==2
    assert director.planning_blocked(result)


def test_cancellation_preserves_checkpoint_and_propagates(isolated,monkeypatch):
    from src.api.task_runtime import TaskCancelled
    g,calls=arrange(monkeypatch,[PASS])
    def cancel():
        if 'create' in calls:raise TaskCancelled('cancelled')
    with pytest.raises(TaskCancelled):create(g,cancel_check=cancel)
    saved=director.load_plan('quality')
    assert len(saved['shots'])==2 and director.planning_blocked(saved)
    assert calls==['create']


def test_repair_structure_error_is_explained_and_preserves_candidate(isolated, monkeypatch):
    bad = shot(0)
    bad['motion_prompt'] = 'opening. action. camera. ending.'
    g, _ = arrange(monkeypatch, [REJECT], {'shots': [bad]})
    p = create(g)
    assert p['error']['category'] == 'invalid_shot_structure'
    assert p['error']['code'] == 'invalid_shot_structure'
    assert '修复格式不合规' in p['error']['safe_message']
    assert '保留原候选' in p['error']['safe_message']
    assert p['shots']['0']['motion_prompt'] == shot(0)['motion_prompt']


@pytest.mark.parametrize('code', ['incomplete_shots', 'invalid_duration_budget', 'invalid_local_patch',
    'changed_shot_duration', 'local_patch_changed_neighbor', 'semantic_review_invalid_schema',
    'semantic_review_invalid_issue', 'semantic_review_invalid_index', 'semantic_review_invalid_json',
    'semantic_review_inconsistent_status', 'semantic_review_missing_shots',
    'semantic_review_global_or_missing_targets', 'semantic_repair_invalid_schema',
    'semantic_repair_wrong_targets', 'semantic_repair_changed_narration_or_duration'])
def test_known_local_errors_keep_safe_codes(isolated, monkeypatch, code):
    g, _ = arrange(monkeypatch, [ValueError(code)])
    p = create(g)
    assert p['error']['category'] == code
    assert p['error']['code'] == code
    assert p['error']['safe_message']


def test_unknown_valueerror_with_secret_remains_redacted(isolated, monkeypatch):
    secret = 'semantic_review_invalid_schema sk-secret-provider-token'
    g, _ = arrange(monkeypatch, [ValueError(secret)])
    p = create(g)
    assert p['error']['category'] != 'semantic_review_invalid_schema'
    assert 'sk-secret-provider-token' not in json.dumps(p)


@pytest.mark.parametrize('message,code', [
    ('分镜图片描述为空', 'image_prompt_empty'),
    ('导演画面契约格式无效', 'visual_contract_invalid'),
    ('分镜主体、景别或文字策略不受支持', 'visual_contract_unsupported'),
    ('完整分镜约束超过服务商提示词长度上限，请由导演精简；不得截断规则', 'image_prompt_over_limit'),
])
def test_compile_errors_have_fixed_safe_codes(isolated, monkeypatch, message, code):
    g, _ = arrange(monkeypatch, [ValueError(message)])
    p = create(g)
    assert p['error']['code'] == code
    assert p['error']['category'] == code
    assert '保留原候选' in p['error']['safe_message']
