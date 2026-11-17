import json
from pathlib import Path
from copy import deepcopy
import pytest
from app.route_packages import _validate
from scripts.configure_reception_20261117 import upgrade,GUIDANCE

ROOT=Path(__file__).resolve().parents[2]/'data/knowledge/china2go/route-releases/reception-20261117'


@pytest.mark.parametrize('path',sorted(ROOT.glob('*.json')),ids=lambda p:p.stem)
def test_all_enabled_packages_are_valid_and_upgrade_is_idempotent(path):
    p=json.loads(path.read_text(encoding='utf8'))
    _validate(deepcopy(p),path)
    assert upgrade(p)==p
    assert p['ai_guidance'].startswith(GUIDANCE)
    if p.get('large_party'):
        assert '或ID' in p['large_party']['contact_text']
        if 'peach_interest_wait' in p['large_party'].get('followup_policy',{}).get('checkpoints',{}):
            assert p['large_party']['followup_policy']['checkpoints']['peach_interest_wait']==60


def test_upgrade_changes_only_guidance_contact_and_checkpoint():
    p={'ai_guidance':'old','content_groups':{'lake':{'initial_delivery':True,'asset_keys':['lake']}},
       'large_party':{'minimum':6,'contact_text':'LINE/微信/WhatsApp QR Code',
           'followup_policy':{'checkpoints':{'peach_interest_wait':5,'daily':1440},'guidance':'五分鐘'}},
       'followup_policy':{'checkpoints':{'fitness_wait':5,'quote_wait':180}},'price':100}
    q=upgrade(p)
    assert q['content_groups']==p['content_groups'] and q['price']==100
    assert q['large_party']['minimum']==6 and q['followup_policy']==p['followup_policy']
    assert q['large_party']['followup_policy']['checkpoints']['daily']==1440
    assert p['large_party']['contact_text'].endswith('QR Code')
