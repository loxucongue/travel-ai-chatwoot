from copy import deepcopy
from pathlib import Path
import json

import pytest

from app.reception_v3 import runtime, service, live
from app.reception_v3.runtime import Decision
from app.reception_v3.seasonal_quotes import departure_quote, month_answer_window, validate_pricing
from app.route_packages import _validate
from scripts.configure_winter_spring_20261017 import update_package, update_config, SPECIALS
from scripts.prepare_winter_route_20261017 import build_package, RID
from test_seasonal_retention import catalog, NOW, PEACH
from test_seasonal_runtime_journeys import runtime_context
from test_tibet_release_20261014 import TEN, EIGHT
from test_reception_v3 import create
from test_reception_v3_skills import model, final
from test_silent_handoff import run_for


def winter_catalog():
    result = catalog()
    result['routes'][RID] = build_package('2026-12-15', '2027-02-28', confirmed=True)
    result['routes'] = {key:update_package(p,confirmed_specials=True) for key,p in result['routes'].items()}
    return result


@pytest.mark.parametrize('route,day,amount',[(r,*v) for r,v in SPECIALS.items()])
def test_exact_cny_dates_override_weekday_winter_and_holiday_but_not_other_dates(route,day,amount):
    bundle=winter_catalog()
    result=departure_quote(bundle,route,day,'',NOW,party_size=2,group_type='shared')
    assert result['status']=='quoted' and result['price_cny']==amount
    assert result['departure_examples']==[day] and result['special_departure']
    assert 'single_supplement_cny' not in result
    assert result['script_id'] in {s['id'] for s in bundle['routes'][route]['fixed_answers']}
    assert departure_quote(bundle,route,'2027-02-07','',NOW)['status']=='holiday'
    assert departure_quote(bundle,route,day,'',NOW,group_type='private')['status']=='advisor'
    assert departure_quote(bundle,route,day,'',NOW,party_size=7)['status']=='advisor'
    assert departure_quote(bundle,route,day,'','2027-01-25T00:00:00Z')['status']=='advisor'
    assert departure_quote(bundle,route,day.replace('2027','2028'),'',NOW)['status']=='unavailable'


def test_cny_month_returns_choices_without_authorizing_monthwide_price():
    result=departure_quote(winter_catalog(),TEN,'2027-02-01','2027-02-28',NOW)
    assert result['status']=='special_options' and 'script_id' not in result
    assert result['departures']==[{'date':'2027-02-10','price_cny':12980}]


def test_winter_normal_price_and_full_year_limits():
    bundle=winter_catalog()
    assert departure_quote(bundle,RID,'2026-12-17','',NOW)['price_cny']==9280
    assert departure_quote(bundle,RID,'2027-02-25','',NOW)['price_cny']==9280
    assert departure_quote(bundle,RID,'2026-12-10','',NOW)['status']=='unavailable'
    assert departure_quote(bundle,RID,'2027-12-16','',NOW)['status']=='unavailable'
    result=departure_quote(bundle,TEN,'2027-01-01','2027-01-31',NOW)
    assert RID in {a['route_variant'] for a in result['alternatives']}
    assert 'winter_recommendation' not in result


def test_pending_winter_is_reviewable_but_not_authorized_for_activation():
    p=build_package('2026-12-15','2027-02-28')
    _validate(p,Path('winter.json'))
    assert not p['publication']['activation_allowed']
    assert not p['seasonal_pricing'].get('special_departures')
    assert all('rongbuk' not in key for g in p['content_groups'].values() for key in g['asset_keys'])


def test_configuration_update_is_narrow_and_idempotent():
    before=catalog()['routes'][PEACH]
    after=update_package(before)
    assert update_package(after)==after
    script=next(s for s in after['fixed_answers'] if s['id']=='departure_question')
    assert '每週' not in script['answer_text'] and '3/20–4/10' in script['answer_text']
    assert before['departure_availability']==after['departure_availability']
    for key in ('knowledge_facts','content_groups','content_sequence'):
        assert before[key]==after[key]
    before=winter_catalog()['routes'][EIGHT]
    assert update_package(before,confirmed_specials=True)==before
    config={'common_scripts':[],'silence':{'daily_at':'09:00'},'routing':{'enabled_route_variants':[PEACH]}}
    assert update_config(config)==config


@pytest.mark.parametrize('text,month',[('四月份',4),('4月',4),('十一月',11),('明年2月',2),('2027年4月',4)])
def test_bare_month_is_persisted_without_selecting_a_cheaper_date(text,month):
    result=month_answer_window(text,NOW)
    assert result['start'][5:7]==f'{month:02d}' and result['customer_month']==text
    assert result['end'].endswith(('30','31','28'))


@pytest.mark.parametrize('text',['不是四月','四月不去','如果四月呢？','4/20','2～3位','4月或5月','2028年4月，但只是比較'])
def test_negative_or_hypothetical_months_are_not_profile_facts(text):
    assert month_answer_window(text,NOW) is None


def test_late_month_pauses_unsent_intro_before_model_and_resumes_ordinary_question(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch)
        row.messages=[]
        v=service.state(row);v.update(opening_started=True)
        service.append_plan(row,v,[{'content':'舊線介紹','content_type':'text'}],'introduction',2)
        service.save(row,v)
        service.add_message(db,row,'4月','late-month')
        assert service.state(row)['departure_review_pending']
        service.advance(row,wall_now=service.later(row.controls['simulation']['last_wall_at'],30))
        assert row.messages[0]['status']=='draft'
        # Live transport must also stop before remote calls or channel submission.
        monkeypatch.setattr(live,'permitted',lambda *a,**k:pytest.fail('paused plan reached transport'))
        live.deliver_one(db,row)
        decision=Decision(route_variant='nine',interrupt=True,messages=[{'text':'四月推薦桃花行程。'}]).model_dump()
        service.apply_decision(db,row,run_for(db,row),service.compile_skills(db),decision)
        assert row.messages[0]['status']=='cancelled'
        assert not service.state(row).get('departure_review_pending')


def test_normal_question_does_not_pause_sop(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch)
        v=service.state(row);v.update(opening_started=True,delivery_kind='introduction');service.save(row,v)
        service.add_message(db,row,'酒店有獨立衛浴嗎？','bathroom')
        assert not service.state(row).get('departure_review_pending')


def test_april_is_checked_before_first_model_call_and_closed_intro_rejected(monkeypatch):
    c=runtime_context(TEN)
    c['messages']=[{'direction':'incoming','content':'4月'}]
    model(monkeypatch,[final(route_variant=TEN,start_introduction=True),
        final(route_variant=TEN,interrupt=True,messages=[{'text':'四月推薦桃花，出發到4/10。'}])])
    decision,logs,_=runtime.run_agent(c)
    assert logs[0]['status']=='quote_rejected'
    assert decision['profile']['departure_window']=='4月'
    assert decision['profile']['_departure_check']['result']['status']=='unavailable'
    assert not decision['start_introduction']


def test_all_winter_media_resolve_in_isolated_catalog_without_production_activation(session_factory,monkeypatch,tmp_path):
    import app.route_packages as registry
    from app.models import KnowledgeVersion
    from app.reception_config import default_reception_configuration,ReceptionConfiguration,put_reception_configuration
    from app.reception_v3.skills import compile_skills,SkillRegistry
    from app.material_library import candidate_materials,resolve_materials
    from scripts.stage_tibet_routes_20261014 import install_materials
    root=Path(__file__).resolve().parents[2]
    release=root/'data/knowledge/china2go/route-releases/20261017-winter'
    manifest=json.loads((release/'asset-manifest.json').read_text(encoding='utf8'))
    files={a['asset_key']:(release/a['relative_path']).read_bytes() for a in manifest}
    original=registry.RUNTIME_PACKAGE_ROOT
    monkeypatch.setattr(registry,'RUNTIME_PACKAGE_ROOT',tmp_path/'routes')
    try:
        registry.install_route_package(update_package(build_package('2026-12-15','2027-02-28',confirmed=True),confirmed_specials=True))
        with session_factory() as db:
            db.add(KnowledgeVersion(id=1,tenant_id=1,version_key=registry.KNOWLEDGE_VERSION,title='Isolated winter',content_hash='winter',source_summary={}))
            db.flush()
            install_materials(db,manifest,files,tmp_path/'media',1,1,1)
            config=default_reception_configuration();config['routing']['enabled_route_variants']=[RID]
            put_reception_configuration(db,ReceptionConfiguration.model_validate(config));db.commit()
            bundle=compile_skills(db)
            assert set(bundle['routes'])=={RID}
            for asset in manifest:
                resolved=resolve_materials(db,[asset['asset_key']],RID,1)
                assert resolved[0]['media_hash']==asset['sha256']
            skills=SkillRegistry(bundle,candidate_materials(db,1))
            assert skills.route_skill(RID)=='tibet-winter-8d'
            body=skills.load('tibet-winter-8d')['instructions']
            assert '10,980' in body and '9,280' in body
            spec=bundle['routes'][RID]
            assert spec['groups'][spec['introduction_sequence'][0]]['assets']==[RID+'_map']
            assert all(not key.startswith('followup-') for key in spec['introduction_sequence'])
    finally:
        monkeypatch.setattr(registry,'RUNTIME_PACKAGE_ROOT',original)
        registry.reload_route_packages()


@pytest.mark.parametrize('window,winter', [('2月',True),('二月份',True),('春節',True),
    ('2027/2/6',True),('2027-02-10',True),('12月到2月',True),('冬天',True),
    ('',None),('日期還沒定',None),('2月或4月',None),('四月',False),('6月',False)])
def test_seasonal_accommodation_does_not_send_rongbuk_photos_in_winter_or_unknown_dates(session_factory,monkeypatch,window,winter):
    from app.reception_v3.seasonal_quotes import seasonal_group
    source=update_package(catalog()['routes'][TEN],confirmed_specials=True)['content_groups']['rongbuk']
    group={'text':source['approved_text'],'assets':source['asset_keys'],
           'seasonal_override':source['seasonal_override'],'delivery_mode':'text_then_assets'}
    expected=seasonal_group(group,{'departure_window':window})
    if winter is False:
        assert expected==group
    else:
        assert expected['assets']==[] and '珠峰小鎮' in expected['text']
        if winter:
            assert expected['text']==source['seasonal_override']['text']
    with session_factory() as db:
        row=create(db,monkeypatch);row.memory={'departure_window':window}
        b=service.compile_skills(db)
        b['routes']['nine']['groups']={'rongbuk':group}
        b['routes']['nine']['introduction_sequence']=['rongbuk']
        monkeypatch.setattr(service,'resolve_materials',lambda db,keys,*a:[{'asset_key':k,'content_type':'image'} for k in keys])
        decision=Decision(route_variant='nine',start_introduction=True).model_dump()
        _,parts=service.parts_for(db,row,b,decision)
        assert [p.get('asset_key') for p in parts if p.get('asset_key')]==(group['assets'] if winter is False else [])
        assert parts[0]['content']==expected['text']
        # Explicit group references and requested follow-up images obey the same season.
        decision=Decision(route_variant='nine',messages=[{'script_id':'rongbuk','asset_keys':group['assets']}]).model_dump()
        _,parts=service.parts_for(db,row,b,decision)
        assert parts[0]['content']==expected['text']
        assert [p.get('asset_key') for p in parts if p.get('asset_key')]==(group['assets'] if winter is False else [])
