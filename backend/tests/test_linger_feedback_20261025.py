"""Real queue/database journeys, with outbound and paid model transport mocked."""
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select, func

from app import route_packages as rp
from app.automation_models import AutomationSession
from app.material_library import candidate_materials
from app.models import KnowledgeVersion, StoredMedia, MaterialAsset
from app.reception_config import MaterialRule
from app.reception_v3 import service, runtime
from app.reception_v3.material_rules import eligible_rules
from app.reception_v3.route_choice import entry_comparison, advertisement_route
from scripts.configure_linger_feedback_20261025 import (
    update_package, update_rules, configure_assets, PHOTOS, ROUTES, EVEREST)
from test_business_feedback_20261025 import packages
from test_reception_v3 import answer, delivered, step
from test_v3_intake_followup import config, create
from test_sticker_ack_20261022 import image_context, model, final, sticker_enabled

ROOT = Path(__file__).resolve().parents[2]


def comparison_bundle():
    spec = config(90)
    spec['routes']['nine']['entry_comparison'] = {
        'other_route':'eleven', 'preferred_route':'eleven', 'summary':'兩條主要差珠峰段2天，先介紹11日。'}
    spec['routes']['eleven']['entry_comparison'] = {
        **spec['routes']['nine']['entry_comparison'], 'other_route':'nine'}
    spec['routes']['eleven']['groups']['map']['text'] = '11日完整行程圖'
    return spec


@pytest.mark.parametrize('first,second', [('九日','十一日'), ('十一日','九日')])
@pytest.mark.parametrize('delivered_intake', [True, False])
def test_two_ad_clicks_keep_one_intake_then_one_long_intro(session_factory, monkeypatch, first, second, delivered_intake):
    spec = comparison_bundle()
    with session_factory() as db:
        row = create(db, monkeypatch, entry=first, wait_seconds=90)
        monkeypatch.setattr(service, 'compile_skills', lambda db:deepcopy(spec))
        if delivered_intake:
            step(db,row,0)
        row.virtual_now = service.later(row.virtual_now,11)
        service.add_message(db,row,second,'second');db.commit()
        assert row.controls['route_variant'] == 'eleven'
        assert not service.state(row).get('intake_answered')
        assert len([m for m in row.messages if m.get('content_type')=='input_select']) == 1
        step(db,row,0)
        # Persist/reload before timeout, as a worker restart must keep the choice.
        db.expire_all(); row = db.get(AutomationSession,row.id)
        step(db,row,90)
        for _ in range(8): step(db,row,2)
        texts = delivered(row)
        assert texts.count('11日完整行程圖') == 1
        assert '完整行程图' not in texts
        assert texts.count('兩條主要差珠峰段2天，先介紹11日。') == 1
        assert service.state(row)['completed_introductions'] == ['eleven']
        # A later real question remains a question; it cannot restart comparison.
        monkeypatch.setattr(service,'run_agent',lambda c:answer(messages=[{'text':'可以說明差別。'}]))
        service.add_message(db,row,'我只有9天，想改看9日','shorter');db.commit()
        step(db,row,0);step(db,row,2)
        assert delivered(row).count('11日完整行程圖') == 1
        assert '可以說明差別。' in delivered(row)


@pytest.mark.parametrize('text,elapsed', [('我只有9天',11), ('不上珠峰，想看九日',11), ('九日',121)])
def test_free_text_or_late_card_is_not_automatic_default(text,elapsed):
    msgs = [{'direction':'incoming','content':'十一日','created_at':'2026-10-25T01:00:00+00:00'},
            {'direction':'incoming','content':text,'created_at':service.later('2026-10-25T01:00:00+00:00',elapsed)}]
    assert entry_comparison(msgs,comparison_bundle()['routes']) is None


def test_disabled_or_inconsistent_pair_does_not_select_route():
    spec = comparison_bundle()['routes']
    msgs = [{'direction':'incoming','content':name,'created_at':'2026-10-25T01:00:00+00:00'}
            for name in ('九日','十一日')]
    assert entry_comparison(msgs,{'nine':spec['nine']}) is None
    spec['eleven']['entry_comparison']['preferred_route'] = 'nine'
    assert entry_comparison(msgs,spec) is None


@pytest.mark.parametrize('route,text', [
    ('tibet_essence_8d','西藏精華-8天 拉薩+珠峰'),
    ('tibet_overview_10d','西藏全覽-10天 林芝+拉薩+珠峰'),
    ('peach_9d_2027','3~4月｜西藏桃花節不上珠峰 9日'),
    ('peach_11d_2027','3~4月｜西藏桃花節＋珠峰 11日'),
])
def test_exact_reported_ad_titles_match(route,text):
    catalog = {k:{'name':p['name'],'aliases':[
        p['selection_title'],p['default_entry_message'],*p.get('selection_aliases',[])]}
        for k,p in packages().items()}
    assert advertisement_route(text,catalog) == route
    assert advertisement_route(text+'，但我只想玩8天',catalog) == ''
    assert advertisement_route('不上珠峰',catalog) == ''


def test_comparison_migration_valid_preserves_product_content():
    for route, original in packages().items():
        updated = update_package(original)
        assert update_package(updated) == updated
        rp._validate(deepcopy(updated),Path(route))
        for field in original.keys() - {'package_version','entry_comparison'}:
            assert updated[field] == original[field]
        rule = updated.get('entry_comparison')
        if rule and route.startswith('tibet_'):
            assert '兩條都有珠峰' in rule['summary'] and '林芝' in rule['summary']


def test_mid_intro_like_guard_keeps_queue_instead_of_handoff(monkeypatch):
    c = image_context()
    c['state'] = {'delivery_kind':'introduction'}
    model(monkeypatch,[final(action='handoff',handoff_type='knowledge')])
    decision,logs,_ = runtime.run_agent(c)
    assert decision['action'] == 'queue' and not decision['interrupt']
    assert not decision['start_introduction'] and not decision['messages']
    assert logs[-1]['status'] == 'acknowledgement_guard'


def test_after_intro_like_can_continue_topic_without_changing_profile(monkeypatch):
    c = image_context()
    c['state'] = {'completed_introductions':[c['route_variant']]}
    text = '沿途的雪山景色也很值得期待，您平常比較喜歡自然風景嗎？'
    model(monkeypatch,[final(messages=[{'text':text}])])
    decision,_,_ = runtime.run_agent(c)
    assert decision['messages'][0]['text'] == text
    assert decision['profile'] == {} and not decision['start_introduction']


def test_like_during_delivery_finishes_existing_plan_once(session_factory,monkeypatch):
    with session_factory() as db:
        row = create(db,monkeypatch,wait_seconds=90)
        step(db,row,0);step(db,row,90);step(db,row,2)
        plan = service.state(row)['delivery_plan_id']
        monkeypatch.setattr(service,'run_agent',lambda c:answer(action='queue' if c['event']=='customer_message' else 'wait'))
        service.add_message(db,row,'👍','like');db.commit();step(db,row,0)
        assert service.state(row)['delivery_plan_id'] == plan
        for _ in range(7):step(db,row,2)
        assert delivered(row).count('完整行程图') == 1
        assert delivered(row).count('住宿原文') == 1
        assert not any(m.get('status')=='cancelled' and m.get('v3_kind')=='introduction' for m in row.messages)


def test_new_media_import_rules_and_dedup(session_factory,tmp_path):
    rules = update_rules([])
    assert len(rules) == 4 and update_rules(rules) == rules
    rules[0]['enabled'] = False
    assert update_rules(rules) == rules
    for rule in rules:MaterialRule.model_validate(rule)
    assert next(r for r in rules if r['id'].endswith('food'))['asset_keys'] == []
    with session_factory() as db:
        db.add(KnowledgeVersion(tenant_id=1,version_key=rp.KNOWLEDGE_VERSION,title='test',content_hash='test'))
        db.flush()
        files = {topic:ROOT/'data/knowledge/china2go/followup-20261025'/p['filename'] for topic,p in PHOTOS.items()}
        imported = configure_assets(db,tmp_path/'media',files)
        db.commit()
        assert configure_assets(db,tmp_path/'media',files) == imported
        assert db.scalar(select(func.count()).select_from(StoredMedia)) == 3
        assert db.scalar(select(func.count()).select_from(MaterialAsset)) == 3
        materials = candidate_materials(db,1)
        for route in ROUTES:
            choices = eligible_rules({'material_rules':rules},materials,route,[],'silence_due',[])
            assert any(r['id'].endswith('stars') for r in choices) == (route in EVEREST)
            golden = next(r for r in choices if r['id'].endswith('golden'))
            assert golden['asset_keys'] == [PHOTOS['golden']['key']]
            photo = next(m for m in materials if m['key']==PHOTOS['golden']['key'])
            history = [{'direction':'outgoing','status':'delivered','media_hash':photo['media_hash']}]
            again = eligible_rules({'material_rules':rules},materials,route,[],'silence_due',history)
            assert next(r for r in again if r['id'].endswith('golden'))['asset_keys'] == []
