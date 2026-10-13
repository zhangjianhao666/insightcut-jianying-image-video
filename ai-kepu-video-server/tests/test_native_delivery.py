"""Native dialogue delivery gates; no paid endpoints."""
import pytest
from src.media import native_delivery as delivery


def test_late_dialogue_trim_keeps_whole_line_and_budget():
    windows = delivery.delivery_windows(
        [5.167, 5.167], [[{'start':1.49,'end':5.09,'text':'鹰嘴豆圆圆的，还有一个小尖嘴。'}],
                             [{'start':.21,'end':4.01,'text':'煮熟后口感软糯，做沙拉很合适。'}]], 10,
    )
    assert windows[0]['start'] < 1.49
    assert windows[0]['end'] >= 5.09
    assert windows[1]['end'] >= 4.01
    assert sum(w['end']-w['start'] for w in windows) < 10


def test_budget_must_not_cut_speech():
    with pytest.raises(delivery.DeliveryError, match='缩短文稿'):
        delivery.delivery_windows([6,6], [[{'start':0,'end':5.9,'text':'长台词'}]]*2, 10)


def test_invalid_speech_timestamps_block_delivery():
    with pytest.raises(delivery.DeliveryError):
        delivery.delivery_windows([5], [[{'start':-1,'end':7,'text':'台词'}]], 10)


def test_empty_speech_and_missing_dialogue_block_delivery():
    with pytest.raises(delivery.DeliveryError, match='对白'):
        delivery.validate_dialogue('介绍鹰嘴豆', [])
    with pytest.raises(delivery.DeliveryError, match='复核'):
        delivery.validate_dialogue('鹰嘴豆圆圆的，还有一个小尖嘴。', [{'text':'大家好欢迎订阅我的频道'}])
    delivery.validate_dialogue('鹰嘴豆圆圆的，还有一个小尖嘴。', [{'text':'英嘴豆圆圆的,还有一个小尖嘴。'}])


def test_subtitles_follow_real_speech_not_clip_start(tmp_path):
    windows=[{'start':.9,'end':5.167}, {'start':0,'end':4.5}]
    speech=[[{'start':1.49,'end':5.09,'text':'第一句'}],[{'start':.21,'end':4.01,'text':'第二句'}]]
    srt,vtt=delivery.write_subtitles(tmp_path, ['第一句','第二句'], speech, windows, [4.267,4.5])
    assert '00:00:00,590 -->' in srt.read_text()
    assert '00:00:04,477 -->' in srt.read_text()
    assert vtt.read_text().startswith('WEBVTT')
    assert len(srt.read_text().strip().split('\n\n'))==2


def test_local_asr_requires_explicit_existing_model(monkeypatch):
    monkeypatch.delenv('NATIVE_ASR_PYTHON', raising=False)
    monkeypatch.delenv('NATIVE_ASR_MODEL_PATH', raising=False)
    with pytest.raises(delivery.DeliveryError, match='本地语音识别'):
        delivery.preflight()


def test_retry_local_delivery_never_dispatches_paid_jobs(monkeypatch, tmp_path):
    import time
    from src.config import Config
    from src.media import video_first
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    image=tmp_path/'frame.png'; image.write_bytes(b'frame')
    clip=tmp_path/'raw.mp4'; clip.write_bytes(b'original')
    segment={'segment_index':0,'text':'介绍鹰嘴豆','image_status':'completed','image_path':str(image)}
    monkeypatch.setattr(video_first.shot_video_jobs, 'duration_options', lambda:(5,))
    monkeypatch.setattr(video_first.shot_video_jobs, 'video_provider', lambda:'wan')
    shots=video_first._shot_inputs('retry', [segment], '16:9', 5)
    video_first._write('retry', {'job_id':'old','status':'delivery_blocked','shots':shots,'ratio':'16:9','duration':5,
                        'audio_mode':'native','output_path':str(tmp_path/'final.mp4'), 'completed':1})
    monkeypatch.setattr(video_first.shot_video_jobs, 'status', lambda *args:{'status':'completed','prompt':shots[0]['motion_prompt'],'duration':5})
    monkeypatch.setattr(video_first.shot_video_jobs,'matching_video_path',lambda *args:str(clip))
    monkeypatch.setattr(video_first.shot_video_jobs,'start',lambda *args,**kw:pytest.fail('paid dispatch during local retry'))
    monkeypatch.setattr(delivery,'preflight',lambda:{})
    def fake_deliver(paths, shots, output, **kwargs):
        assert paths == [str(clip)]
        kwargs['stage_callback']('transcribing')
        Path(output).write_bytes(b'final')
        return {'delivery_duration':4.5,'has_audio':True}
    from pathlib import Path
    monkeypatch.setattr(delivery,'deliver',fake_deliver)
    monkeypatch.setattr(video_first,'_register_delivery_assets',lambda *args:None)
    result=video_first.retry_delivery('retry',[segment],ratio='16:9',subtitles=True,max_duration=5)
    for _ in range(100):
        result=video_first.status('retry',[segment],ratio='16:9')
        if result['status']=='completed':break
        time.sleep(.01)
    assert result['status']=='completed'
    assert result['delivery_duration']==4.5
    assert clip.read_bytes()==b'original'


def test_unavailable_delivery_preflight_does_not_submit_paid(monkeypatch, tmp_path):
    from src.config import Config
    from src.media import video_first
    monkeypatch.setattr(Config,'BASE_DIR',tmp_path)
    monkeypatch.setattr(delivery,'preflight',lambda:(_ for _ in ()).throw(delivery.DeliveryError('本地语音识别未配置')))
    monkeypatch.setattr(video_first.shot_video_jobs,'start',lambda *a,**kw:pytest.fail('paid call before preflight'))
    with pytest.raises(video_first.VideoFirstError,match='本地语音识别'):
        video_first.start('new',[],confirmed=True,subtitles=True,max_duration=10)


def test_delivery_registered_for_delete_and_shutdown(monkeypatch, tmp_path):
    import threading
    from src.config import Config
    from src.media import video_first
    from src.api.task_runtime import task_runtime
    from pathlib import Path
    monkeypatch.setattr(Config,'BASE_DIR',tmp_path)
    image=tmp_path/'frame.png';image.write_bytes(b'frame')
    clip=tmp_path/'raw.mp4';clip.write_bytes(b'raw')
    seg={'segment_index':0,'text':'鹰嘴豆','image_status':'completed','image_path':str(image)}
    monkeypatch.setattr(video_first.shot_video_jobs,'video_provider',lambda:'wan')
    monkeypatch.setattr(video_first.shot_video_jobs,'duration_options',lambda:(5,))
    shots=video_first._shot_inputs('lifecycle',[seg],'16:9',5)
    video_first._write('lifecycle',{'job_id':'j','status':'delivery_blocked','shots':shots,'ratio':'16:9','duration':5,'output_path':str(tmp_path/'out.mp4')})
    monkeypatch.setattr(video_first.shot_video_jobs,'status',lambda *a:{'status':'completed','prompt':shots[0]['motion_prompt'],'duration':5})
    monkeypatch.setattr(video_first.shot_video_jobs,'matching_video_path',lambda *a:str(clip))
    monkeypatch.setattr(delivery,'preflight',lambda:{})
    entered=threading.Event();release=threading.Event()
    def wait_deliver(*a,**kw):
        entered.set();release.wait(5);kw['stage_callback']('publishing')
        Path(a[2]).write_bytes(b'final');return {'has_audio':True}
    monkeypatch.setattr(delivery,'deliver',wait_deliver)
    monkeypatch.setattr(video_first,'_register_delivery_assets',lambda *a:None)
    try:
        video_first.retry_delivery('lifecycle',[seg])
        assert entered.wait(3)
        assert task_runtime.is_running('lifecycle')
        task_runtime.request_cancel('lifecycle')
        assert not task_runtime.wait_until_stopped('lifecycle',.01)
    finally:
        release.set()
        assert task_runtime.wait_until_stopped('lifecycle',3)
    assert video_first._load('lifecycle')['status']!='completed'


def test_real_ffmpeg_caption_delivery_preserves_originals(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg required')
    ff=delivery._ffmpeg()
    folder=tmp_path/"quotes' 中文";folder.mkdir()
    paths=[]
    for i in range(2):
        p=folder/f'raw{i}.mp4'
        subprocess.run([ff,'-v','error','-y','-f','lavfi','-i','color=c=blue:s=320x180:d=1.4',
                        '-f','lavfi','-i','sine=frequency=440:duration=1.4','-c:v','libx264','-c:a','aac',str(p)],check=True)
        paths.append(p)
    originals=[p.read_bytes() for p in paths]
    out=folder/'final.mp4';out.write_bytes(b'previous-delivery')
    stages=[]
    result=delivery.deliver(paths,[{'text':'第一句'},{'text':'第二句'}],out,max_duration=3,
                            runtime={'ffmpeg':ff},recognizer=lambda p:[{'start':.1,'end':1.1,'text':'第一句' if Path(p).stem=='raw0' else '第二句'}],
                            stage_callback=stages.append)
    assert result['delivery_duration']<=3
    assert delivery.probe(out)[1]=={'video','audio'}
    assert [p.read_bytes() for p in paths]==originals
    assert Path(result['subtitle_path']).is_file()
    assert (Path(result['delivery_path']).parent/'previous_published.mp4').read_bytes()==b'previous-delivery'
    assert stages[-1]=='publishing'


def test_delivery_failure_preserves_existing_published_output(tmp_path, monkeypatch):
    from pathlib import Path
    raw=tmp_path/'raw.mp4';raw.write_bytes(b'raw')
    out=tmp_path/'published.mp4';out.write_bytes(b'existing-good')
    monkeypatch.setattr(delivery,'probe',lambda _: (5,{'video','audio'}))
    with pytest.raises(delivery.DeliveryError,match='复核'):
        delivery.deliver([raw],[{'text':'鹰嘴豆'}],out,max_duration=5,runtime={'ffmpeg':'unused'},recognizer=lambda p:[{'start':1,'end':4,'text':'欢迎关注我的频道'}])
    assert out.read_bytes()==b'existing-good'
    assert raw.read_bytes()==b'raw'
    assert len(list((tmp_path/'deliveries').iterdir()))==1


def test_h3_missing_director_cannot_submit_paid(monkeypatch, tmp_path):
    from src.config import Config
    from src.media import video_first
    monkeypatch.setattr(Config,'BASE_DIR',tmp_path)
    monkeypatch.setattr(video_first.shot_video_jobs,'video_provider',lambda:'minimax')
    monkeypatch.setattr(video_first.shot_video_jobs,'_video_config',lambda:{'model':'MiniMax-H3','provider':'minimax'})
    monkeypatch.setattr(video_first.shot_video_jobs,'duration_options',lambda:(5,))
    monkeypatch.setattr(video_first.shot_video_jobs,'status',lambda *a:{'status':'not_generated'})
    monkeypatch.setattr(video_first.shot_video_jobs,'start',lambda *a,**kw:pytest.fail('missing director paid call'))
    monkeypatch.setattr(delivery,'preflight',lambda:{})
    image=tmp_path/'frame.png';image.write_bytes(b'image')
    seg={'segment_index':0,'text':'鹰嘴豆','image_path':str(image),'image_status':'completed'}
    with pytest.raises(video_first.VideoFirstError,match='导演规划'):
        video_first.start('unplanned',[seg],duration=5,confirmed=True)


def test_created_subtitles_registered_before_later_render_failure(tmp_path, monkeypatch):
    from pathlib import Path
    raw=tmp_path/'raw.mp4';raw.write_bytes(b'raw')
    out=tmp_path/'output.mp4'
    monkeypatch.setattr(delivery,'probe',lambda _: (5,{'video','audio'}))
    registered=[]
    def fake_run(command, **kwargs):
        if '-vf' in command and 'subtitles=' in command[command.index('-vf')+1]:
            raise delivery.DeliveryError('burn failed')
        if command[-1].endswith('.mp4'):
            Path(command[-1]).write_bytes(b'video')
        return ''
    monkeypatch.setattr(delivery,'_run',fake_run)
    with pytest.raises(delivery.DeliveryError):
        delivery.deliver([raw],[{'text':'鹰嘴豆'}],out,max_duration=6,runtime={'ffmpeg':'fake'},
                         recognizer=lambda p:[{'start':.2,'end':4,'text':'鹰嘴豆'}],asset_callback=registered.append)
    assert any(Path(a['subtitle_path']).is_file() for a in registered if 'subtitle_path' in a)
    assert any(Path(a['vtt_path']).is_file() for a in registered if 'vtt_path' in a)
    assert not out.exists()


@pytest.mark.parametrize('expected,recognized',[
    ('鹰嘴豆圆圆的，还有一个小尖嘴。它富含蛋白质。','鹰嘴豆圆圆的，还有一个小尖嘴。'),
    ('鹰嘴豆圆圆的，还有一个小尖嘴。','鹰嘴豆圆圆的，还有一个小尖嘴。记得关注哦。'),
])
def test_whole_missing_or_extra_short_sentence_requires_review(expected,recognized):
    with pytest.raises(delivery.DeliveryError,match='复核'):
        delivery.validate_dialogue(expected,[{'text':recognized}])


def test_queued_delivery_cancel_releases_lifecycle_without_running(tmp_path,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from src.config import Config
    from src.media import video_first
    from src.api.task_runtime import task_runtime
    monkeypatch.setattr(Config,'BASE_DIR',tmp_path)
    video_first._write('queued',{'status':'transcribing'})
    token=task_runtime.begin('queued');assert token
    block=threading.Event(); entered=threading.Event()
    def occupied():entered.set();block.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(occupied);assert entered.wait(1)
        video_first._ACTIVE.add('queued')
        video_first._launch('queued',lambda:pytest.fail('cancelled queued worker ran'),token,pool)
        try:
            assert task_runtime.request_cancel('queued')
            assert task_runtime.wait_until_stopped('queued',1)
            assert video_first._load('queued')['status']=='interrupted'
        finally:block.set()


def test_long_native_subtitles_use_speech_cues_and_fit_two_lines(tmp_path):
    from src.utils.subtitle_text import normalize_subtitle_text
    first='脂肪含量不算高，而且以不饱和脂肪酸为主。'
    second='可以把它看作日常饮食里一种很实在的植物性食材，但不必神化成超级食物。'
    speech=[[{'start':1,'end':5,'text':first},{'start':6,'end':13,'text':second}]]
    srt,vtt=delivery.write_subtitles(tmp_path,[first+second],speech,[{'start':0,'end':14}],[14])
    blocks=srt.read_text().strip().split('\n\n')
    assert len(blocks)>=2
    assert '00:00:01,000 --> 00:00:05,000' in blocks[0]
    assert '00:00:06,000 -->' in blocks[1]
    texts=[]
    for block in blocks:
        lines=block.splitlines()[2:]
        assert 1<=len(lines)<=2
        assert all(len(line)<=20 for line in lines)
        texts.extend(lines)
    assert ''.join(texts)==normalize_subtitle_text(first+second)
    assert '\n'.join(texts) in vtt.read_text() or all(line in vtt.read_text() for line in texts)
