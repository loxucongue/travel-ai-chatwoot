import pytest


from app.config import settings


from app.operations import save_setting


from app.reception_config import get_reception_configuration, live_silence_enabled


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


def test_opening_media_never_invents_question():
    from app.opening_messages import delivery_items
    items = [{'key':'hello','content_type':'text','content':'您好'}, {'key':'photo','content_type':'image','media_id':1}]
    assert delivery_items(items, []) == items
