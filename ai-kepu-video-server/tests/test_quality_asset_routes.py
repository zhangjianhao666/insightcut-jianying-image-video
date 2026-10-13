import asyncio
import pytest
from fastapi import HTTPException, Response
from src.api import routes
from src.text import storyboard_director as director


@pytest.fixture
def rejected(monkeypatch):
    monkeypatch.setattr(routes.mysql_client,'get_task',lambda _: {'voice_confirmed':True})
    monkeypatch.setattr(routes.mysql_client,'get_segments',lambda _: [{'segment_index':0,'text':'旁白','image_prompt':'A blue ball','image_path':''}])
    monkeypatch.setattr(director,'load_plan',lambda _: {'review_required':True,'status':'reviewing'})
    monkeypatch.setattr(routes,'_plan_fingerprint',lambda *args:'snapshot')
    monkeypatch.setattr(routes.task_executor,'continue_task',lambda _:pytest.fail('must not dispatch images'))


def test_confirm_assets_rejects_candidate_before_dispatch(rejected):
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.generate_task_workspace_assets('test',Response(),{'snapshot_key':'snapshot'}))
    assert error.value.status_code==409 and '审查' in error.value.detail


def test_image_retry_rejects_candidate_before_creating_operation(rejected,monkeypatch):
    monkeypatch.setattr(routes,'_resolve_retry_targets',lambda *args:[{'segment_index':0,'asset_type':'image'}])
    monkeypatch.setattr(routes.mysql_client,'get_active_task_operation',lambda _:pytest.fail('must gate before operation'))
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.retry_task_assets('test',Response(),{'snapshot_key':'snapshot'}))
    assert error.value.status_code==409 and '审查' in error.value.detail


def test_rejected_visual_plan_does_not_block_existing_audio_operation(rejected,monkeypatch):
    targets=[{'segment_index':0,'asset_type':'audio'}]
    monkeypatch.setattr(routes,'_resolve_retry_targets',lambda *args:targets)
    monkeypatch.setattr(routes.mysql_client,'get_active_task_operation',lambda _: {'kind':'retry_assets','snapshot_key':'snapshot','targets':targets})
    monkeypatch.setattr(routes,'_workspace_operation_payload',lambda op:{'reused':True})
    response=Response()
    assert asyncio.run(routes.retry_task_assets('test',response,{'snapshot_key':'snapshot'}))=={'reused':True}
    assert response.status_code==200


def test_manual_prompt_edit_invalidates_required_review(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.config import Config
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    candidate={'version':director._PLAN_VERSION,'review_required':True,'status':'completed',
               'segments':['旁白'],'visual_bible':{},'shots':{'0':{'image_prompt':'old'}},
               'quality':{'version':director._QUALITY_VERSION,'status':'passed'}}
    candidate['quality']['candidate_digest']=director._candidate_digest(candidate)
    director._write('edit',candidate)
    monkeypatch.setattr(routes.task_manager,'get_task',lambda _:SimpleNamespace(voice_type='voice'))
    monkeypatch.setattr(routes.mysql_client,'get_task',lambda _: {})
    monkeypatch.setattr(routes,'_ensure_workspace_mutable',lambda _:None)
    monkeypatch.setattr(routes.mysql_client,'get_segments',lambda _:[{'segment_index':0,'text':'旁白','image_prompt':'old'}])
    monkeypatch.setattr(routes.mysql_client,'update_segment_plan',lambda *a,**kw:2)
    monkeypatch.setattr(routes.task_manager,'invalidate_task_cache',lambda _:None)
    monkeypatch.setattr(routes,'_plan_fingerprint',lambda *a:'snapshot')
    asyncio.run(routes.update_segment('edit',0,image_prompt='new',text=None,image_path=None,image_url=None,audio_url=None,audio_voice_type=None,audio_tts_options=None,expected_plan_version=1))
    saved=director.load_plan('edit')
    assert saved['shots']['0']['image_prompt']=='new'
    assert director.planning_blocked(saved)
    assert saved['quality']['status']=='pending'


def test_review_context_changes_invalidate_passed_digest():
    p={'review_required':True,'status':'completed','shots':{},'segments':['旁白'],
       'visual_bible':{},'review_context':{'style':'2D','ratio':'16:9'},
       'quality':{'version':director._QUALITY_VERSION,'status':'passed'}}
    p['quality']['candidate_digest']=director._candidate_digest(p)
    assert not director.planning_blocked(p)
    p['review_context']['style']='3D'
    assert director.planning_blocked(p)
