import importlib.util
from pathlib import Path

from app.reception_config import ReceptionConfiguration
from app.reception_v3.skills import SkillRegistry

spec = importlib.util.spec_from_file_location('contact_update', Path(__file__).parents[1] / 'scripts/update_contact_reception.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def test_contact_update_preserves_authored_opening_channels_silence_and_custom_scripts():
    before = ReceptionConfiguration().model_dump()
    before['reply']['custom_guidance'] = '自定义要求保留\n线路产品不匹配转人工：客户想了解的项目没有匹配到的时候；客户想了解的项目没有匹配到的时候，直接转人工'
    before['common_scripts'] = [{'id': 'contact_email', 'name': '自有邮件话术', 'scenario': '邮件联系', 'text': '自己的原话', 'enabled': True}]
    after = update.updated_configuration(before)
    assert after['reply']['opening_message'] == before['reply']['opening_message']
    assert after['reply']['custom_guidance'] == '自定义要求保留'
    assert after['silence'] == before['silence']
    assert after['lead_capture']['channels'] == before['lead_capture']['channels']
    assert after['common_scripts'][0]['text'] == '自己的原话'
    assert after['routing']['outside_catalog_action'] == 'consult_advisor'
    assert not after['lead_capture']['require_supported_route']
    assert update.updated_configuration(after) == after
    validated = ReceptionConfiguration.model_validate(after).model_dump()
    loaded = SkillRegistry({**validated, 'routes': {}}).load('tibet-reception')['instructions']
    assert '自己的原话' in loaded and 'consult_advisor' in loaded
