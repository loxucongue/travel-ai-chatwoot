from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.reception_v3 import large_party, profile_labels, service
from app.route_packages import _validate
from scripts.update_ali_north_central_20261105 import ALI, update_package
from test_ali_series_20261102 import group_bundle
from test_large_party_reception import setup, drain
from test_reception_v3 import step

RELEASE = Path(__file__).resolve().parents[2] / 'data/knowledge/china2go/route-releases/20261105-ali-north-central'


def package():
    return json.loads((RELEASE / 'route-package.json').read_text(encoding='utf8'))


def test_release_preserves_prices_and_changes_daily_itinerary():
    p = package()
    _validate(p, RELEASE / 'route-package.json')
    old = json.loads((RELEASE.parent / '20261102-ali/route-package.json').read_text(encoding='utf8'))
    assert p['seasonal_pricing'] == old['seasonal_pricing']
    assert update_package(p) == p
    facts = {f['id']: f['text'] for f in p['knowledge_facts']}
    days = facts[ALI + '.daily_itinerary']
    assert '尼瑪雲天洲際' not in days
    assert 'D12\n洞措' in days and '那倉卓姆莊園' in days
    assert 'D13\n尼瑪 → 達則措' in days
    assert days.index('恰規措') < days.index('吳如措') < days.index('色林措')
    assert p['followup_policy']['daily_sequence'][-1] == 'followup_wenbu'
    assert len(p['followup_policy']['daily_sequence']) == 8
    assert '官方LINE' not in p['large_party']['contact_text']


@pytest.mark.parametrize('size,private,expected', [(5,False,False),(6,False,True),(7,False,True),(4,True,True)])
def test_ali_boundary_and_other_routes_unchanged(size,private,expected):
    p=package()
    bundle={'routes':{ALI:p,'ordinary':{'large_party':{**p['large_party'],'minimum':7}}}}
    profile={'party_size':size,'group_type':'private' if private else 'shared'}
    assert bool(large_party.policy_for(bundle,ALI,profile)) is expected
    if size==6 and not private:
        assert large_party.policy_for(bundle,'ordinary',profile) is None


@pytest.mark.parametrize('size,group,label',[(6,'shared','高價值・阿里獨立成團'),(4,'private','高價值・阿里訂製')])
def test_intro_to_notification_uses_configured_label_once(session_factory,monkeypatch,size,group,label):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size':size,'group_type':group},completed=True)
        spec=group_bundle()
        spec['routes']['nine']['large_party'].update(minimum=6,include_explicit_private=True,
            handoff_label='高價值・阿里獨立成團',explicit_private_label='高價值・阿里訂製')
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(spec))
        step(db,row);drain(db,row)
        assert row.controls['evaluation_labels']==[label]
        assert service.state(row)['group_advisor_pending']
        assert not large_party.finish(db,row)


def test_model_profile_labels_do_not_scan_customer_text(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'guest_segment':'experienced'})
        profile_labels.apply(db,row,package())
        profile_labels.apply(db,row,package())
        assert row.controls['evaluation_labels']==['高價值・深度玩家','高價值・阿里訂製']
        row.controls={};row.memory={'guest_segment':['experienced']}
        profile_labels.apply(db,row,package())
        assert not row.controls


def test_material_order_matches_change_list():
    p=package();seq=p['content_sequence'];groups=p['content_groups']
    assert seq[:5]==['itinerary_overview','north_central_loop','wenbu_village','wenbu_manor','wenbu_sunset']
    assert seq[seq.index('north_central_lakes'):seq.index('wildlife')+1]==['north_central_lakes','wuma','dangra_lake','wildlife']
    assert groups['wenbu_village']['asset_keys']==['ali15_wenbu_village']
    assert groups['wenbu_manor']['delivery_mode']=='text_then_assets'
    manifest=json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    assert len(manifest)==5 and all(s['source_filename'].endswith('.png') for s in manifest)
