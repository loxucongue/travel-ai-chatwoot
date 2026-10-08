from copy import deepcopy
import json

import pytest
from app.route_packages import PACKAGE_ROOT, _validate
from app.reception_v3.skills import compile_skills, SkillRegistry
from scripts.update_route_prices_20271002 import update_package


@pytest.mark.parametrize('days,price,single', [(9,'10,780','2,700'),(11,'12,780','3,200')])
def test_price_update_preserves_operator_content_and_reaches_compiled_skill(session_factory, days, price, single):
    path = PACKAGE_ROOT / f'peach-{days}d-2027' / 'route-package.json'
    original = json.loads(path.read_text(encoding='utf8'))
    original['content_groups']['contact_request']['approved_text'] = '运营自定义LINE邀请'
    original['initial_delivery_interval_seconds'] = 5
    original['content_groups']['price_reference']['approved_text'] = '旧价格'
    updated = update_package(original)
    _validate(deepcopy(updated), path)
    assert updated['content_groups']['contact_request'] == original['content_groups']['contact_request']
    assert updated['initial_delivery_interval_seconds'] == 5
    assert updated['content_sequence'] == original['content_sequence']
    for key, group in original['content_groups'].items():
        if key not in ('price_reference','price_deferral'):
            assert updated['content_groups'][key] == group
    assert update_package(updated) == updated
    with session_factory() as db:
        bundle = compile_skills(db)
        compiled = SkillRegistry(bundle).load(f'peach-{days}d-2027')['instructions']
    assert price in compiled and single in compiled
    for old in ('9,980','11,480','2,600','59880'):
        assert old not in compiled
    assert '6人' in compiled and '4人獨立成團' in compiled


def test_unrelated_route_unchanged():
    other = {'route_variant':'other_route','content_groups':{'price':{'approved_text':'9980'}}}
    assert update_package(other) == other
