"""Local product, pricing and conversation execution acceptance; no paid model."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from app.route_packages import PACKAGE_ROOT, ROUTE_PACKAGES, _validate, RoutePackageError
from app.reception_v3 import runtime, service
from app.reception_v3.route_choice import LINZHI, choice_error
from app.reception_v3.seasonal_quotes import departure_quote
from app.reception_v3.skills import SkillRegistry
from app.model_transport import EvaluationCallError
from test_reception_v3 import create
from test_reception_v3_skills import context as empty_context, model, final, tool
from test_silent_handoff import run_for
from test_seasonal_retention import catalog

RELEASE = PACKAGE_ROOT.parent / 'route-releases/20261020-linzhi8'
PACKAGE_PATH = RELEASE / 'tibet-linzhi-8d/route-package.json'
PACKAGE = json.loads(PACKAGE_PATH.read_text(encoding='utf8'))
ASSETS = json.loads((RELEASE / 'asset-manifest.json').read_text(encoding='utf8'))
TEN = 'tibet_overview_10d'
NOW = '2026-10-20T12:00:00+00:00'


def compile_package(p):
    return {'name':p['name'], 'aliases':p.get('selection_aliases',[]), 'version':p['package_version'],
        'facts':p['knowledge_facts'], 'scripts':p['fixed_answers'], 'intake':p.get('intake'),
        'seasonal_pricing':p.get('seasonal_pricing'), 'departure_availability':p.get('departure_availability'),
        'groups':{k:{'text':g['approved_text'], 'assets':g.get('asset_keys',[]), 'delivery_mode':g.get('delivery_mode','assets_then_text')}
                  for k,g in p['content_groups'].items()},
        'introduction_sequence':[k for k in p['content_sequence'] if p['content_groups'][k].get('initial_delivery')],
        'switch_introduction_sequence':p.get('switch_introduction_sequence'), 'interval_seconds':2}


def material_catalog():
    return [{'key':a['asset_key'], 'routes':[LINZHI], 'content_type':'image', 'media_hash':a['sha256'],
             'media_id':1000+i, 'what_it_shows':a['what_it_shows']}
            for i,a in enumerate(ASSETS)] + [{'key':'everest-old', 'routes':[TEN], 'content_type':'image',
                                            'media_hash':'everest-old'}]


def ctx(route=TEN, event='customer_message'):
    c = empty_context()
    c.update(now=NOW, route_variant=route, event=event, profile={}, state={'opening_started':True},
             available_materials=material_catalog())
    c['skills']['routes'] = {k:compile_package(p) for k,p in catalog()['routes'].items()}
    c['skills']['routes'][LINZHI] = compile_package(deepcopy(PACKAGE))
    c['skills']['intake'] = {'enabled':True, 'question':'人数', 'options':['2位'], 'wait_seconds':30}
    c['skills']['reply'] = {'opening_messages':['您好～我是China2Go的旅遊顧問～很高興認識你'],
                            'opening_interval_seconds':2, 'opening_items':[]}
    c['skills']['silence'] = {'enabled':True, 'daily_at':'09:00', 'timezone':'Asia/Taipei',
                             'intervals_minutes':[1440], 'active_start':'09:00','active_end':'21:00'}
    c['skills']['digest'] = 'isolated-linzhi-acceptance'
    return c


def setup_row(db, monkeypatch, c):
    row = create(db, monkeypatch)
    row.controls = {'route_variant':c['route_variant'], 'simulation':{'status':'running','last_wall_at':NOW},
                    'v3':{'opening_started':True, **c['state']}, 'human':False, 'labels':['ai','以開口']}
    row.memory = deepcopy(c['profile'])
    row.virtual_now = NOW
    row.messages = deepcopy(c.get('messages', []))
    monkeypatch.setattr(service, 'compile_skills', lambda *_:deepcopy(c['skills']))
    monkeypatch.setattr(service, 'candidate_materials', lambda *_:deepcopy(c['available_materials']))
    by_key = {m['key']:m for m in c['available_materials']}
    def resolve(db, keys, route, tenant):
        assert all(route in by_key[k]['routes'] for k in keys)
        return [{**by_key[k], 'asset_key':k, 'content_type':'image'} for k in keys]
    monkeypatch.setattr(service, 'resolve_materials', resolve)
    return row


def test_candidate_is_complete_and_not_in_default_catalog():
    p = _validate(deepcopy(PACKAGE), PACKAGE_PATH)
    assert LINZHI not in ROUTE_PACKAGES
    assert p['publication']['activation_allowed'] is False
    assert p['publication']['requires_user_approval'] is True
    assert p['knowledge_version'] == next(iter(ROUTE_PACKAGES.values()))['knowledge_version']
    assert len(ASSETS) == 18
    assert {a['asset_key'] for a in ASSETS} == {k for g in p['content_groups'].values() for k in g['asset_keys']}
    for a in ASSETS:
        data = (RELEASE / a['path']).read_bytes()
        assert len(data) == a['byte_size'] and hashlib.sha256(data).hexdigest() == a['sha256']
    text = (RELEASE/'source/business-page.txt').read_text(encoding='utf8')
    assert hashlib.sha256(text.encode()).hexdigest() == p['source']['snapshot_sha256']
    for answer in p['fixed_answers']:
        if answer['id'].startswith('price_'):
            assert all(line in text for line in answer['answer_text'].splitlines())
            assert '單人房差' not in answer['answer_text']
            assert '包含' in answer['answer_text'] and '不含' in answer['answer_text']
    initial = ''.join(g['approved_text'] for g in p['content_groups'].values() if g['initial_delivery'])
    assert all(s not in initial for s in ('移動氧艙','12小時','85-90','不會高反喔，放心','Amber','LINE','¥'))
    assert len(p['switch_introduction_sequence']) == 2


@pytest.mark.parametrize('bad', [[], ['missing'], ['itinerary_overview']*2, 'itinerary_overview', [None]])
def test_switch_sequence_validation(bad):
    p = deepcopy(PACKAGE); p['switch_introduction_sequence'] = bad
    with pytest.raises(RoutePackageError, match='switch_sequence'):
        _validate(p, PACKAGE_PATH)


@pytest.mark.parametrize('start,end,expected,amount', [
    ('2027-11-02','','quoted',8280), ('2027-06-01','','quoted',9580),
    ('2027-08-03','','quoted',9980), ('2027-03-09','','quoted',8280),
    ('2027-03-11','2027-04-30','unavailable',None), ('2027-04-20','','unavailable',None),
    ('2027-05-01','','holiday',None), ('2027-09-25','2027-10-14','holiday',None),
    ('2027-12-07','','advisor',None), ('2027-01-05','','advisor',None),
    ('2027-02-05','','holiday',None), ('2028-06-01','','unavailable',None),
    ('2027-06-29','2027-07-06','ask_date',None), ('unknown','','ask_date',None),
])
def test_exact_2027_price_and_sale_boundaries(start, end, expected, amount):
    result = departure_quote(ctx()['skills'], LINZHI, start, end, NOW, party_size=2)
    assert result['status'] == expected
    assert result.get('price_cny') == amount
    if expected == 'advisor':
        assert result['reason_code'] == 'business_confirmation_required'
    if start == '2027-04-20':
        assert not any(a['route_variant'].startswith('peach_') for a in result['alternatives'])


def test_full_calendar_prices_do_not_borrow_another_eight_day_product():
    for i in range(365):
        d = date(2027,1,1) + timedelta(days=i)
        result = departure_quote(ctx()['skills'], LINZHI, d.isoformat(), '', NOW, party_size=2)
        md = d.strftime('%m-%d')
        if '03-11' <= md <= '04-30':
            expected = 'unavailable'
        elif any(a <= md <= b for a,b in [('02-01','02-16'),('04-25','05-05'),('09-25','10-14')]):
            expected = 'holiday'
        elif d.month in (1,2,12):
            expected = 'advisor'
        else:
            expected = 'quoted' if d.weekday() == 1 else 'ask_date'
        assert result['status'] == expected, (d,result)
        if expected == 'quoted':
            assert result['price_cny'] == (9580 if d.month in (5,6) else 9980 if d.month in (7,8,9) else 8280)
        else:
            assert 'price_cny' not in result


def test_private_date_notice_and_room_supplement():
    b = ctx()['skills']
    assert departure_quote(b,LINZHI,'2027-06-02','',NOW,party_size=4)['status'] == 'private_departure'
    assert departure_quote(b,LINZHI,'2027-06-02','',NOW,party_size=2)['status'] == 'ask_date'
    assert departure_quote(b,LINZHI,'2027-06-01','','2027-05-03T12:00:00Z')['status'] == 'advisor'
    r = departure_quote(b,LINZHI,'2027-06-01','','2027-05-02T12:00:00Z')
    assert r['price_cny'] == 9580 and r['single_supplement_cny'] == 2200


@pytest.mark.parametrize('title', PACKAGE['selection_aliases'])
def test_advertisement_exact_match_enters_new_product_once(session_factory, monkeypatch, title):
    c = ctx('')
    with session_factory() as db:
        row = setup_row(db, monkeypatch, c)
        row.controls = {**row.controls,'v3':{}}
        service.add_message(db,row,title,'ad')
        assert row.controls['route_variant'] == LINZHI
        assert service.state(row)['intake_field'] == 'departure_window'
        assert [m['content'] for m in row.messages if m.get('status') == 'draft'] == ['請問您大概想幾月出發呢？']
        service.add_message(db,row,title,'ad')  # Webhook duplicate.
        assert len([m for m in row.messages if m.get('status') == 'draft']) == 1


@pytest.mark.parametrize('question', ['珠峰會不會高反？','帶爸爸去','想看珠峰但怕高反','車上有氧氣嗎'])
def test_concern_does_not_change_product(session_factory, monkeypatch, question):
    c = ctx()
    c['messages'] = [{'direction':'incoming','content':question}]
    model(monkeypatch,[final(route_variant=TEN, profile={'altitude_concern':True},
                             messages=[{'text':'高原反應因人而異，供氧也不能保證不會高反。'}])])
    decision, _, _ = runtime.run_agent(c)
    with session_factory() as db:
        row = setup_row(db,monkeypatch,c)
        service.apply_decision(db,row,run_for(db,row),c['skills'],decision)
        assert row.controls['route_variant'] == TEN
        assert row.memory['altitude_concern'] is True
        assert 'everest_preference' not in row.memory


def test_unaccepted_route_change_is_repaired_and_double_failure_is_not_sendable(monkeypatch):
    c = ctx()
    bad = final(route_variant=LINZHI,start_introduction=True)
    good = final(route_variant=TEN,route_choice='recommend',profile={'discussion_route':LINZHI},
                 messages=[{'text':'也可以看看不上珠峰的林芝8日～'}])
    requests = model(monkeypatch,[bad,good,good])
    d,logs,_ = runtime.run_agent(c)
    assert len(requests) == 3 and d['route_variant'] == TEN and not d['start_introduction']
    assert logs[0]['status'] == 'route_choice_rejected'
    assert logs[1]['status'] == 'skill_activation'
    model(monkeypatch,[bad,bad])
    with pytest.raises(EvaluationCallError,match='route_choice_not_validated'):
        runtime.run_agent(c)


def test_reject_everest_pauses_before_model_then_recommendation_cancels_drafts(session_factory,monkeypatch):
    c = ctx()
    with session_factory() as db:
        row = setup_row(db,monkeypatch,c)
        value = service.state(row)
        service.append_plan(row,value,[{'content':'珠峰介紹','content_type':'text'},
            {'content':'','content_type':'image','asset_key':'everest-old'}],'introduction',2)
        service.save(row,value)
        old_ids = {m['id'] for m in row.messages}
        service.add_message(db,row,'不上珠峰，有別的行程嗎？','reject')
        assert service.state(row)['route_review_pending']
        service.advance(row,service.later(NOW,60))
        assert all(m['status'] == 'draft' for m in row.messages if m['id'] in old_ids)
        d = runtime.Decision(route_variant=TEN,route_choice='recommend',interrupt=True,
            profile={'everest_preference':'avoid','discussion_route':LINZHI},
            messages=[{'text':'可以看看不上珠峰的林芝8日～','asset_keys':[LINZHI+'_itinerary']}]).model_dump()
        service.apply_decision(db,row,run_for(db,row),c['skills'],d)
        assert all(m['status'] == 'cancelled' for m in row.messages if m['id'] in old_ids)
        assert row.controls['route_variant'] == TEN
        assert row.memory['discussion_route'] == LINZHI
        assert not service.state(row).get('route_review_pending')
        assert service.followup_materials(material_catalog(),row.messages,TEN,row.memory) == []
        silent = ctx(TEN,'silence_due'); silent['profile'] = row.memory
        wrong = runtime.Decision(route_variant=TEN,messages=[{'text':'珠峰星空很好看'}]).model_dump()
        assert choice_error(wrong,silent)
        assert choice_error(runtime.Decision(action='wait',route_variant=TEN).model_dump(),silent) is None


def test_accepted_switch_preserves_slots_ownership_and_deduplicates_map(session_factory,monkeypatch):
    c = ctx()
    c['profile'] = {'departure_window':'2027年6月','party_size':2,'relationship':'夫妻',
                    'contact_preference':'在这里聊','discussion_route':LINZHI,'everest_preference':'avoid',
                    '_departure_check':{'route':TEN}}
    c['state'].update(completed_introductions=[TEN],introduction_route=TEN)
    c['messages'] = [{'id':'map-offer','direction':'outgoing','status':'sent','content_type':'image',
                      'asset_key':LINZHI+'_itinerary','media_hash':ASSETS[0]['sha256']}]
    with session_factory() as db:
        row = setup_row(db,monkeypatch,c)
        ownership = {k:deepcopy(row.controls[k]) for k in ('human','labels')}
        d = runtime.Decision(route_variant=LINZHI,route_choice='selected',start_introduction=True,interrupt=True).model_dump()
        service.apply_decision(db,row,run_for(db,row),c['skills'],d)
        assert row.controls['route_variant'] == LINZHI
        assert row.memory['party_size'] == 2 and row.memory['departure_window'] == '2027年6月'
        assert row.memory['contact_preference'] == '在这里聊'
        assert 'discussion_route' not in row.memory and '_departure_check' not in row.memory
        assert {k:row.controls[k] for k in ownership} == ownership
        drafts = [m for m in row.messages if m.get('status') == 'draft']
        assert len(drafts) == 2 and all(m['content_type'] == 'text' for m in drafts)
        assert not any('幾月' in m['content'] or '幾位' in m['content'] or '您好' in m['content'] for m in drafts)
        assert not any(m.get('asset_key') == 'everest-old' for m in drafts)


def test_customer_can_choose_everest_again_but_timer_cannot_choose(session_factory,monkeypatch):
    c = ctx(LINZHI)
    c['profile'] = {'everest_preference':'avoid','party_size':2}
    d = runtime.Decision(route_variant=TEN,route_choice='selected',profile={'everest_preference':'want'},
                         interrupt=True,start_introduction=True).model_dump()
    assert choice_error(d,c) is None
    c['event'] = 'silence_due'
    assert choice_error(d,c)


def test_seasonal_quote_tool_template_reaches_delivery_without_room_fee(session_factory,monkeypatch):
    c = ctx(LINZHI)
    model(monkeypatch,[{'tool_calls':[tool('get_departure_quote',
        {'route':LINZHI,'start':'2027-06-01','end':'2027-06-30'})]},
        final(route_variant=LINZHI,messages=[{'script_id':'price_summer'},
              {'text':'您這次是跟家人還是朋友一起來呢？'}])])
    d,_,_ = runtime.run_agent(c)
    with session_factory() as db:
        row = setup_row(db,monkeypatch,c)
        _,parts = service.parts_for(db,row,c['skills'],d)
        assert '9,580' in parts[0]['content'] and '單人房差' not in parts[0]['content']
        assert '不含' in parts[0]['content'] and len(parts) == 2


def test_new_skill_enabled_only_for_test_catalog():
    registry = SkillRegistry(ctx()['skills'],material_catalog())
    assert registry.route_skill(LINZHI) == 'tibet-linzhi-8d'
    assert '不能保證不會高反' in registry.load('tibet-linzhi-8d')['instructions']
    assert LINZHI not in ROUTE_PACKAGES


def test_live_sender_cannot_send_old_photo_during_route_review(session_factory, monkeypatch):
    from app.reception_v3 import live
    c = ctx()
    with session_factory() as db:
        row = setup_row(db,monkeypatch,c)
        v = service.state(row)
        service.append_plan(row,v,[{'content':'','content_type':'image','asset_key':'everest-old'}],'introduction',2)
        v['route_review_pending'] = True
        service.save(row,v)
        def forbidden(*a,**k):
            raise AssertionError('sender must stop before channel access')
        monkeypatch.setattr(live,'permitted',forbidden)
        live.deliver_one(db,row)
        assert row.messages[-1]['status'] == 'draft'


def test_known_date_rechecked_after_product_change_and_closed_product_never_starts(monkeypatch):
    c = ctx(TEN)
    c['profile'] = {'departure_window':'2027年4月20日',
                   '_departure_check':{'route':TEN,'start':'2027-04-20','end':'2027-04-20'}}
    c['messages'] = [{'direction':'incoming','content':'那就換這條不上珠峰的8天'}]
    bad = final(route_variant=LINZHI,route_choice='selected',start_introduction=True,interrupt=True)
    good = final(route_variant=TEN,interrupt=True,messages=[{'text':'這個日期林芝8日尚未開放，您時間方便調整嗎？'}])
    requests = model(monkeypatch,[bad,bad,good])
    d,logs,_ = runtime.run_agent(c)
    assert not d['start_introduction']
    assert any(log.get('status') == 'quote_rejected' for log in logs)
    assert len(requests) == 3


def test_changed_date_cannot_reuse_old_month_evidence(monkeypatch):
    c = ctx(TEN)
    c['profile'] = {'departure_window':'2027年6月',
                   '_departure_check':{'route':TEN,'start':'2027-06-01','end':'2027-06-30'}}
    c['messages'] = [{'direction':'incoming','content':'那改成明年4月20日的林芝8天吧'}]
    bad = final(route_variant=LINZHI,route_choice='selected',start_introduction=True,interrupt=True,
                profile={'departure_window':'2027年4月20日'})
    model(monkeypatch,[bad,bad,{'tool_calls':[tool('get_departure_quote',
         {'route':LINZHI,'start':'2027-04-20','end':'2027-04-20'})]},
         final(route_variant=TEN,interrupt=True,messages=[{'text':'這個日期林芝8日尚未開放。'}])])
    d,logs,_ = runtime.run_agent(c)
    assert not d['start_introduction'] and any(l.get('status')=='quote_rejected' for l in logs)
