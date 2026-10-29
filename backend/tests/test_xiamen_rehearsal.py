"""The supplied FAQ is a scoped campaign; the reusable company base stays empty."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / 'businesses/rujing2/campaigns/xiamen-20261029'


def test_prepare_real_company_materials_in_isolated_playground(tmp_path):
    output = tmp_path / 'rehearsal'
    command = [sys.executable, str(ROOT / 'backend/scripts/rehearse_rujing2_xiamen.py'),
               'prepare', '--output', str(output)]
    result = subprocess.run(command, env={**os.environ, 'DEEPSEEK_API_KEY': ''},
                            capture_output=True, text=True, encoding='utf8', timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['outbound'] is False
    from app.schemas import LoginRequest
    login = LoginRequest.model_validate(json.loads((output / 'login.json').read_text(encoding='utf8')))
    assert login.email == 'rehearsal@rujing2.example.com'
    repeated = subprocess.run(command, capture_output=True, text=True, encoding='utf8', timeout=60)
    assert repeated.returncode != 0 and 'existing_rehearsal_database' in repeated.stderr
    import sqlite3
    with sqlite3.connect(output / 'playground.db') as db:
        assert db.execute('select count(*) from material_assets').fetchone()[0] == 8
        assert db.execute("select count(*) from stored_media where media_type='video'").fetchone()[0] == 2
        config = json.loads(db.execute("select value from app_settings where key='route_reception_config'").fetchone()[0])
    assert config['routing']['enabled_route_variants'] == ['xiamen_3_6d']
    assert config['routing']['live_inbox_ids'] == []
    assert not config['handoff']['large_group_enabled']
    assert not config['silence']['enabled']
    assert '廈門' in config['reply']['opening_items'][0]['content']
    bundle = json.loads((output / 'compiled-skills.json').read_text(encoding='utf8'))
    assert bundle['currency'] == 'TWD'
    assert len(bundle['routes']) == 1
    assert len(bundle['routes']['xiamen_3_6d']['scripts']) == 29
    assert not bundle['routes']['xiamen_3_6d'].get('party_pricing')
    assert not bundle['routes']['xiamen_3_6d'].get('seasonal_pricing')
    source = json.loads((PACK / 'faq.json').read_text(encoding='utf8'))
    assert len(source['items']) == 35
    assert all(item['question'] and item['original_answer'] and item['source_ref'] for item in source['items'])


@pytest.mark.parametrize('text', ['這部分會請顧問協助核對。', '可以請顧問確認後回覆您。', '我會請顧問協助。'])
def test_new_company_does_not_rewrite_advisor_referrals_into_broken_sentences(monkeypatch, text):
    from app.reception_v3 import service_voice
    monkeypatch.setitem(service_voice.PROFILE, 'rewrite_advisor_referrals', False)
    assert service_voice.service_voice(text) == text


def test_empty_reply_cannot_silently_drop_an_intended_handoff():
    from app.reception_v3.runtime import Decision
    with pytest.raises(ValueError, match='empty_reply_with_handoff_reason'):
        Decision(action='reply', messages=[], handoff_reason='Need document review', handoff_type='knowledge')
    assert Decision(action='handoff', handoff_reason='Need document review').action == 'handoff'
    # Explicit wait and an introduction-only plan remain valid.
    assert Decision(action='wait').action == 'wait'
    assert Decision(action='reply', start_introduction=True).start_introduction
