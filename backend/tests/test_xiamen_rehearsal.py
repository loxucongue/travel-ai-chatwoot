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
    assert config['silence']['enabled']
    assert not config['silence']['live_enabled']
    assert config['silence']['daily_at'] == '10:00'
    assert config['silence']['max_proactive_messages_per_day'] == 1
    assert not config['intake']['enabled']
    assert not config['lead_capture']['require_party_size']
    assert not config['lead_capture']['require_departure_window']
    assert len(config['material_rules']) == 8
    assert '廈門' in config['reply']['opening_items'][0]['content']
    bundle = json.loads((output / 'compiled-skills.json').read_text(encoding='utf8'))
    assert bundle['currency'] == 'TWD'
    assert len(bundle['routes']) == 1
    assert len(bundle['routes']['xiamen_3_6d']['scripts']) == 35
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


def test_campaign_video_followup_resolves_real_media_and_deduplicates(tmp_path):
    from test_business_isolation import run_isolated
    from test_xiamen_install import BOOTSTRAP
    run_isolated(tmp_path, BOOTSTRAP + '''
result = installer.install(apply=True)
from app.automation_models import AutomationSession
from app.reception_v3 import service
from app.reception_v3.skills import compile_skills
from app.reception_v3.runtime import Decision
with SessionLocal() as db:
    bundle = compile_skills(db)
    row = AutomationSession(owner_id=9, inbox_binding_id=result['inbox_id'],
        environment='playground', mode='journey', engine_version='v3',
        controls={'route_variant':'xiamen_3_6d'}, memory={}, messages=[])
    db.add(row); db.flush()
    decision = Decision(messages=[{'material_rule_id':'xiamen-followup-meal_video'}]).model_dump()
    _, parts = service.parts_for(db, row, bundle, decision, followup=True)
    assert [p['content_type'] for p in parts] == ['text','video']
    assert parts[1]['asset_key'] == 'xiamen_meal_video'
    assert parts[1]['media_id'] and parts[1]['media_hash']
    row.messages = [{**p, 'direction':'outgoing','status':'delivered'} for p in parts]
    db.commit(); db.expire_all()
    _, repeated = service.parts_for(db, row, bundle, decision, followup=True)
    assert [p['content_type'] for p in repeated] == ['text']
    # A renamed catalog entry must not bypass hash deduplication.
    row.messages = [{'direction':'outgoing','status':'read','media_hash':parts[1]['media_hash']}]
    _, renamed = service.parts_for(db, row, bundle, decision, followup=True)
    assert [p['content_type'] for p in renamed] == ['text']
    raw = Decision(messages=[{'asset_keys':['xiamen_travelers_video']}]).model_dump()
    try:
        service.parts_for(db, row, bundle, raw, followup=True)
        raise AssertionError('must require a configured rule for videos')
    except ValueError as exc:
        assert str(exc) == 'followup_requires_material_rule'
''')
