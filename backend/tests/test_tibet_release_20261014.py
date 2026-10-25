import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.route_packages import PACKAGE_ROOT, ROUTE_PACKAGES, _validate, RoutePackageError
from app.reception_v3.seasonal_quotes import departure_quote, quote_error
from app.reception_v3 import service
from app.reception_v3.runtime import Decision

RELEASE=PACKAGE_ROOT.parent/'route-releases/20261014'
PACKAGES={p['route_variant']:p for f in RELEASE.glob('*/route-package.json') for p in [json.loads(f.read_text(encoding='utf8'))]}
EIGHT='tibet_essence_8d'
TEN='tibet_overview_10d'


def bundle():
    return {'routes':{k:{'seasonal_pricing':p['seasonal_pricing'],'intake':p['intake'], 'scripts':p['fixed_answers']}
                      for k,p in PACKAGES.items()},'intake':{'enabled':True,'question':'人数','options':['2人'],'wait_seconds':120}}


@pytest.mark.parametrize('route,start,end,status,price',[
    (TEN,'2027-03-02','2027-03-02','quoted',10480),
    (TEN,'2027-06-01','2027-06-01','quoted',12280),
    (TEN,'2027-08-03','2027-08-03','quoted',12880),
    (EIGHT,'2027-11-04','2027-11-04','quoted',7980),
    (EIGHT,'2027-03-18','2027-03-18','quoted',9480),
    (EIGHT,'2027-06-03','2027-06-03','quoted',9480),
    (EIGHT,'2027-08-05','2027-08-05','quoted',9980),
    (TEN,'2027-06-01','2027-06-30','quoted',12280),
    (EIGHT,'2027-09-24','2027-10-20','holiday',None),
    (TEN,'2027-05-01','2027-05-31','holiday',None),
    (TEN,'2027-02-01','2027-02-16','holiday',None),
    (TEN,'2027-03-11','2027-03-19','unavailable',None),
    (TEN,'2027-04-11','2027-04-24','unavailable',None),
    (EIGHT,'2027-04-21','2027-04-24','unavailable',None),
    (EIGHT,'2027-04-25','2027-05-05','holiday',None),
    (TEN,'2027-06-29','2027-07-02','ask_date',None),
    (EIGHT,'2027-06-01','2027-06-01','ask_date',None),
    (TEN,'2028-06-01','2028-06-30','unavailable',None),
    (TEN,'2027-12-01','2027-12-31','unavailable',None),
    (EIGHT,'unknown','','ask_date',None),
    (TEN,'2027-06-30','2027-06-01','ask_date',None),
])
def test_departure_date_price_boundaries(route,start,end,status,price):
    result=departure_quote(bundle(),route,start,end,'2026-10-14T03:00:00+00:00')
    assert result['status']==status
    assert result.get('price_cny')==price


def test_notice_30_days_is_not_the_peach_offer_expiry():
    assert departure_quote(bundle(),TEN,'2027-06-01','2027-06-01','2027-05-03T00:00:00+00:00')['status']=='advisor'
    assert departure_quote(bundle(),TEN,'2027-06-01','2027-06-01','2027-05-02T00:00:00+00:00')['status']=='quoted'


@pytest.mark.parametrize('route',[EIGHT,TEN])
def test_release_source_copy_assets_and_inactive_catalog(route):
    package=_validate(deepcopy(PACKAGES[route]),Path('release.json'))
    source=PACKAGE_ROOT.parent/'website-1013-20261014/content.html'
    assert hashlib.sha256(source.read_bytes()).hexdigest()==package['source']['snapshot_sha256']
    text=source.with_name('content.txt').read_text(encoding='utf8')
    assert route not in ROUTE_PACKAGES  # Preparation cannot activate live reception.
    for answer in package['fixed_answers']:
        if answer['answer_origin']=='website_verbatim':
            assert all(line in text for line in answer['answer_text'].splitlines())
    groups=package['content_groups']
    first_wave=''.join(g['approved_text'] for g in groups.values() if g['initial_delivery'])
    assert all(token not in first_wave for token in ['85-90','12小時','移動氧艙','2025年','LINE'])
    for key in ('followup_sakya','followup_railway'):
        assert groups[key]['asset_keys'] and not groups[key]['initial_delivery']
    manifest=json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    declared={m['asset_key'] for m in manifest if m['route_variant']==route}
    refs={a for g in groups.values() for a in g['asset_keys']}
    assert declared==refs


def test_seasonal_price_requires_date_tool_and_exact_template():
    d=Decision(route_variant=TEN,messages=[{'script_id':'price_peak'}]).model_dump()
    assert quote_error(d,bundle(),set())
    assert quote_error(d,bundle(),{(TEN,'price_low')})
    assert quote_error(d,bundle(),{(TEN,'price_peak')}) is None
    d['messages'][0]['text']='更優惠只要999元'
    assert quote_error(d,bundle(),{(TEN,'price_peak')})
    d['messages'][0]={'script_id':'','text':'團費人民幣999元/人'}
    assert quote_error(d,bundle(),set())


@pytest.mark.parametrize('change',[{'wait_seconds':0},{'field':'arbitrary'},{'options':[]}])
def test_bad_intake_config_rejected(change):
    p=deepcopy(PACKAGES[TEN]);p['intake'].update(change)
    with pytest.raises(RoutePackageError):_validate(p,Path('bad.json'))


def test_missing_price_script_rejected():
    p=deepcopy(PACKAGES[TEN]);p['fixed_answers']=[]
    with pytest.raises(RoutePackageError,match='pricing_script_missing'):_validate(p,Path('bad.json'))


def row(memory=None):
    return SimpleNamespace(memory=memory or {},controls={},messages=[],virtual_now='2026-10-14T03:00:00+00:00')


def test_new_route_intake_month_30s_preserves_old_party_120s():
    b=bundle();r=row({'party_size':2});v={}
    assert service.begin_intake(r,v,b,TEN)
    assert v['intake_wait_seconds']==30 and v['intake_field']=='departure_window'
    service.save(r,v);service.complete_delivery(r,r.messages[0]['id'],'simulated_delivered')
    assert service.state(r)['intake_deadline']=='2026-10-14T03:00:30+00:00'
    assert not service.begin_intake(row({'departure_window':'2027年6月'}),{},b,TEN)
    b['routes']['peach_11d_2027']={}
    r=row();v={}
    assert service.begin_intake(r,v,b,'peach_11d_2027')
    assert v['intake_wait_seconds']==120 and v['intake_field']=='party_size'


def test_new_route_seven_people_cannot_start_automated_introduction(monkeypatch):
    b=bundle();r=row({'party_size':7});r.environment='playground';r.generation=1;r.due_at=None
    run=SimpleNamespace(trace={},decision={},input_snapshot={'event':'customer_message'})
    monkeypatch.setattr(service,'update_profile',lambda *args:False)
    monkeypatch.setattr(service,'tenant_for_session',lambda *args:1)
    monkeypatch.setattr(service,'candidate_materials',lambda *args:[])
    service.apply_decision(None,r,run,b,Decision(route_variant=TEN,start_introduction=True).model_dump())
    assert service.state(r)['handoff'] and r.controls['human']
    assert not r.messages


def test_sent_photo_hash_excludes_alias_from_another_route():
    manifest=json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    materials=[{'key':m['asset_key'],'routes':[m['route_variant']],'media_hash':m['sha256'],'content_type':'image'} for m in manifest]
    old=next(m for m in materials if m['key']==TEN+'_sakya')
    sent=[{'direction':'outgoing','status':'sent','asset_key':old['key'],'media_hash':old['media_hash']}]
    offered=service.followup_materials(materials,sent,EIGHT)
    assert EIGHT+'_sakya' not in {m['key'] for m in offered}
    assert EIGHT+'_railway' in {m['key'] for m in offered}


@pytest.mark.parametrize('text', [
    '8天是7,980元/人。', '這條行程每人7980元。',
    '團費是 7980 元。', '團費 RMB 7,980。',
    '費用是 CNY 7,980 /人。', '這條線¥7,980/人。',
    '人民幣¥ 7,980。', '費用為 RMB 7,980。', '這條行程只要7980元。',
    '單房差人民幣2,000元、團費人民幣7,980元。',
])
def test_free_text_price_cannot_bypass_seasonal_date_check(text):
    decision = Decision(route_variant=EIGHT, messages=[{'text': text}]).model_dump()
    assert quote_error(decision, bundle(), set())


@pytest.mark.parametrize('text', [
    '司機和導遊小費各30元，每天合計60元。',
    '2027年司機和導遊小費每天每人60元。',
    '司機和導遊小費60元/人/天。',
    '單人房差為人民幣2,000元。',
    '單房差是 RMB 2,000。', '您提到機票約人民幣3,000元。', '單人房差2,000元/人。',
    '有購物行為賠償人民幣5,000元。', '您提到預算為人民幣10,000元。',
    '您這次預計跟家人還是朋友一起出發呢？',
])
def test_specific_fee_answers_and_question_are_not_whole_trip_quotes(text):
    decision = Decision(route_variant=EIGHT, messages=[{'text': text}]).model_dump()
    assert quote_error(decision, bundle(), set()) is None


@pytest.mark.parametrize('price', [7980, 9480, 9980, 10480, 12280, 12880])
def test_effect_checker_catches_new_route_prices(price):
    from scripts.evaluate_reception_journeys import has_unrequested_price
    assert has_unrequested_price(f'每人{price:,}元', bundle())
    assert has_unrequested_price(f'團費{price}元', bundle())
    assert not has_unrequested_price('您這次預計跟家人還是朋友一起出發呢？', bundle())
