from copy import deepcopy
import pytest
from sqlalchemy import select
from app.models import InboxBinding, AppSetting, AuditLog, User
from app.operations import save_setting
from app.reception_config import get_reception_configuration
from app.reception_rollout import reception_inbox_ids


def seed(factory):
    with factory() as db:
        db.add_all([InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=100, name='咨询频道', ai_enabled=False),
                    InboxBinding(id=2, tenant_id=1, chatwoot_inbox_id=200, name='聊天频道', ai_enabled=True),
                    InboxBinding(id=3, tenant_id=1, chatwoot_inbox_id=300, name='旧频道', status='inactive', ai_enabled=False)])
        save_setting(db, 'route_reception_config', {'routing': {'live_inbox_ids': [100]}, 'intake': {'wait_seconds': 90}})
        save_setting(db, 'global_message_sending', {'enabled': True})
        db.commit()


def test_both_gates_saved_and_reflected_without_enabling_other_channels(authenticated, session_factory):
    client, csrf = authenticated; seed(session_factory)
    before = client.get('/v1/settings/channel-ai').json()
    assert [i['enabled'] for i in before['items']] == [False, False, False]
    with session_factory() as db: original = deepcopy(get_reception_configuration(db))
    response = client.patch('/v1/settings/channel-ai', headers={'X-CSRF-Token': csrf}, json={
        'revision': before['revision'], 'changes': [{'id': 1, 'enabled': True}]})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved == client.get('/v1/settings/channel-ai').json()
    assert [i['enabled'] for i in saved['items']] == [True, False, False]
    with session_factory() as db:
        assert reception_inbox_ids(db) == [100] and db.get(InboxBinding, 1).ai_enabled
        assert get_reception_configuration(db) == original
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'channel_ai.updated'))
    response = client.patch('/v1/settings/channel-ai', headers={'X-CSRF-Token': csrf}, json={
        'revision': saved['revision'], 'changes': [{'id': 1, 'enabled': False}]})
    assert response.status_code == 200
    with session_factory() as db:
        assert reception_inbox_ids(db) == [] and not db.get(InboxBinding, 1).ai_enabled


@pytest.mark.parametrize('changes,status', [
    ([{'id':1,'enabled':True},{'id':3,'enabled':True}],422),
    ([{'id':1,'enabled':True},{'id':999,'enabled':True}],404),
    ([{'id':1,'enabled':True},{'id':1,'enabled':False}],422),
    ([{'id':1,'enabled':'true'}],422),
])
def test_invalid_batch_never_partially_updates(authenticated, session_factory, changes, status):
    client,csrf=authenticated; seed(session_factory)
    before=client.get('/v1/settings/channel-ai').json()
    response=client.patch('/v1/settings/channel-ai',headers={'X-CSRF-Token':csrf},json={'revision':before['revision'],'changes':changes})
    assert response.status_code==status
    assert client.get('/v1/settings/channel-ai').json()==before


def test_stale_save_and_csrf_are_rejected(authenticated, session_factory):
    client,csrf=authenticated;seed(session_factory)
    before=client.get('/v1/settings/channel-ai').json()
    data={'revision':before['revision'],'changes':[{'id':2,'enabled':True}]}
    assert client.patch('/v1/settings/channel-ai',json=data).status_code==403
    with session_factory() as db:
        row=db.get(InboxBinding,2);row.ai_enabled=False;db.commit()
    assert client.patch('/v1/settings/channel-ai',headers={'X-CSRF-Token':csrf},json=data).status_code==409


def test_global_pause_remains_paused_and_admin_required(authenticated, session_factory):
    client,csrf=authenticated;seed(session_factory)
    with session_factory() as db:save_setting(db,'global_message_sending',{'enabled':False});db.commit()
    before=client.get('/v1/settings/channel-ai').json()
    result=client.patch('/v1/settings/channel-ai',headers={'X-CSRF-Token':csrf},json={
        'revision':before['revision'],'changes':[{'id':2,'enabled':True}]}).json()
    assert result['items'][1]['enabled'] and not result['global_sending_enabled']
    with session_factory() as db:
        admin=db.scalar(select(User));admin.role='agent';db.commit()
    assert client.get('/v1/settings/channel-ai').status_code==403
    assert client.patch('/v1/settings/channel-ai',headers={'X-CSRF-Token':csrf},json={
        'revision':result['revision'],'changes':[{'id':2,'enabled':False}]}).status_code==403
