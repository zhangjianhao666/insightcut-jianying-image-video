"""Approved director prompts commit together and preserve local assets."""
import pytest
from src.database import sqlite_client as database_module
from src.database.sqlite_client import SQLiteClient
from src.text.image_contract import MARKER


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'local.db')
    client = SQLiteClient()
    client.create_task('sync', '正文', 'style', 100, execution_mode='review_first')
    image = tmp_path / 'old.png'
    image.write_bytes(b'image')
    client.save_segments('sync', [
        {'segment_index': 0, 'text': '第一句', 'image_prompt': 'old', 'image_path': str(image), 'image_status': 'completed', 'audio_path': 'voice.wav', 'audio_status': 'completed'},
        {'segment_index': 1, 'text': '第二句', 'image_prompt': 'same' + MARKER + '\ncompiled', 'image_status': 'completed'},
        {'segment_index': 2, 'text': '第三句', 'image_prompt': 'old2', 'image_status': 'pending'},
    ])
    return client


def sync(db, prompts, version=0, texts=None):
    return db.sync_director_image_prompts('sync', prompts, expected_plan_version=version, expected_segments=texts or ['第一句', '第二句', '第三句'])


def test_sync_changes_only_raw_differences_preserves_assets_and_canonical_suffix(db):
    assert sync(db, {0: 'new', 1: 'same', 2: 'new2'}) == 1
    rows = db.get_segments('sync')
    assert rows[0]['image_prompt'] == 'new'
    assert rows[0]['image_status'] == 'stale'
    assert rows[0]['image_path'] and rows[0]['audio_path'] == 'voice.wav'
    assert rows[0]['audio_status'] == 'completed'
    assert rows[0]['prompt_status'] == 'completed' and rows[0]['image_error'] is None
    assert rows[1]['image_prompt'] == 'same' + MARKER + '\ncompiled'
    assert rows[1]['image_status'] == 'completed'
    assert rows[2]['image_status'] == 'pending'
    assert sync(db, {0: 'new', 1: 'same'}, version=1) == 1


@pytest.mark.parametrize('version,texts', [(1, None), (0, ['已编辑', '第二句', '第三句'])])
def test_sync_rejects_version_or_narration_conflict(db, version, texts):
    assert sync(db, {0: 'new'}, version, texts) == -1
    assert db.get_segments('sync')[0]['image_prompt'] == 'old'
    assert db.get_task('sync')['plan_version'] == 0


def test_mid_batch_database_failure_rolls_back_all_prompts(db):
    conn = db._get_conn()
    conn.execute("CREATE TRIGGER fail_second_prompt BEFORE UPDATE OF image_prompt ON task_segments WHEN NEW.segment_index=2 BEGIN SELECT RAISE(ABORT, 'test'); END")
    conn.commit()
    conn.close()
    assert sync(db, {0: 'new', 2: 'new2'}) is None
    assert db.get_segments('sync')[0]['image_prompt'] == 'old'
    assert db.get_task('sync')['plan_version'] == 0
