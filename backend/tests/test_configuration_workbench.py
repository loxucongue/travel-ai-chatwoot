import pytest

from app.config import settings
from app.operations import save_setting
from app.reception_config import get_reception_configuration, policy_from_configuration, live_silence_enabled
from app.reception_v2.runtime import _messages
from app.reception_v2.skill_registry import SkillRegistry


def patch(client, csrf, payload):
    return client.patch('/v1/automation/reception-config', json=payload, headers={'X-CSRF-Token': csrf})


def test_route_toggle_does_not_overwrite_new_opening(authenticated):
    client, csrf = authenticated
    items = [{'key':'greeting','content_type':'text','content':'新的開場，照原文發送。'}]
    assert patch(client, csrf, {'reply':{'opening_items':items}}).status_code == 200
    assert patch(client, csrf, {'routing':{'enabled_route_variants':['peach_9d_2027']}}).status_code == 200
    config = client.get('/v1/automation/reception-config').json()['config']
    assert config['reply']['opening_items'][0]['content'] == items[0]['content']
    assert config['routing']['enabled_route_variants'] == ['peach_9d_2027']


def test_common_scripts_and_channel_reach_agent(authenticated, session_factory):
    client, csrf = authenticated
    scripts = [{'id':'common','name':'集合咨询','scenario':'客户问集合','text':'這是已配置的集合話術。','enabled':True},
               {'id':'disabled','name':'停用','scenario':'不使用','text':'停用話術不應加入提示','enabled':False}]
    response = patch(client, csrf, {'common_scripts': scripts, 'lead_capture':{'channels':['WhatsApp'], 'enabled':False}})
    assert response.status_code == 200, response.text
    with session_factory() as db:
        config = get_reception_configuration(db)
    messages = _messages({'module':'reply','customer_text':'集合在哪裏','context_messages':[],
                          'reception_policy':policy_from_configuration(config)}, SkillRegistry())
    assert scripts[0]['text'] in messages[0]['content']
    assert scripts[1]['text'] not in messages[0]['content']
    assert 'WhatsApp' in messages[0]['content']


def test_live_silence_switch_overrides_migration_default(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    monkeypatch.setattr(settings, 'live_sop_enabled', False)
    with session_factory() as db:
        assert not live_silence_enabled(db)
    assert patch(client, csrf, {'silence':{'live_enabled':True}}).status_code == 200
    with session_factory() as db:
        assert live_silence_enabled(db)
    assert patch(client, csrf, {'silence':{'enabled':False}}).status_code == 200
    with session_factory() as db:
        assert not live_silence_enabled(db)


def test_old_custom_rules_migrate_to_visible_guidance_once(session_factory):
    with session_factory() as db:
        save_setting(db, 'route_reception_config', {'schema_version':5,'business_rules':[
            {'id':'custom_rule','name':'特殊需求','condition':'客户问目录外线路','guidance':'请顾问继续处理','enabled':True}]})
        config = get_reception_configuration(db)
        assert '请顾问继续处理' in config['reply']['custom_guidance']
        assert all(rule.get('system_key') for rule in config['business_rules'])
        save_setting(db, 'route_reception_config', config)
        assert get_reception_configuration(db)['reply']['custom_guidance'] == config['reply']['custom_guidance']


@pytest.mark.parametrize('path', ['/v1/sops','/v1/wakeup/policies','/v1/settings/ai-adapter','/v1/settings/ai','/v1/automation/reply-policy'])
def test_retired_configuration_endpoints_removed(authenticated, path):
    client, _ = authenticated
    assert client.get(path).status_code == 404


def test_timing_is_available_in_system_settings(authenticated):
    client, csrf = authenticated
    current = client.get('/v1/settings/reply-timing').json()
    response = client.patch('/v1/settings/reply-timing', json={**current, 'merge_wait_seconds':3}, headers={'X-CSRF-Token':csrf})
    assert response.status_code == 200, response.text
    assert client.get('/v1/settings/reply-timing').json()['merge_wait_seconds'] == 3


def test_real_engine_metadata_and_reject_obsolete_fields(authenticated):
    from app.reception_v2 import ENGINE_RELEASE_ID
    client, csrf = authenticated
    response = client.get('/v1/automation/reception-config').json()
    assert response['version']['label'] == ENGINE_RELEASE_ID
    assert 'model_nodes' not in response
    assert patch(client, csrf, {'stage_journey':{'mandatory_send':True}}).status_code == 422


def test_opening_media_never_invents_question():
    from app.opening_messages import delivery_items
    items = [{'key':'hello','content_type':'text','content':'您好'}, {'key':'photo','content_type':'image','media_id':1}]
    assert delivery_items(items, []) == items


def test_media_only_opening_reaches_live_delivery_without_added_text(session_factory, tmp_path, monkeypatch):
    from test_live_reply import setup
    from test_opening_media import media
    from app import live_reply
    from app.reception_v2.runtime import run_v2_agent
    from app.reception_config import ReceptionConfiguration
    from app.models import OutboundMessage
    from sqlalchemy import select
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        item = media(db, tmp_path)
    config = ReceptionConfiguration(reply={'opening_items': [item]}).model_dump()
    from app.config import settings
    monkeypatch.setattr(settings, 'deepseek_api_key', 'test')
    monkeypatch.setattr('app.reception_v2.runtime._call', lambda *_a, **_kw: ({'content': '{"action":"reply","reply":"greeting","v2_events":[]}'}, {'round':0,'duration_ms':1}))
    result = run_v2_agent({'module':'reply','customer_text':'你好','context_messages':[],
                          'reception_policy':policy_from_configuration(config)})
    assert result[0].reply == ''
    monkeypatch.setattr(live_reply, 'generate_decision', lambda _: result)
    live_reply.process_job(1)
    with session_factory() as db:
        rows = db.scalars(select(OutboundMessage)).all()
        assert [(row.content_type, row.content) for row in rows] == [('image', '')]


def test_retired_adapter_setting_cannot_disable_current_reception(session_factory, monkeypatch):
    from test_live_reply import setup
    from app import live_reply
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        save_setting(db, 'ai_adapter', {'enabled':False})
        db.commit()
    live_reply.process_job(1)
    assert fake.sent
