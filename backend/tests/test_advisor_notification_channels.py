import logging
import pytest
from sqlalchemy import select
from app import advisor_notification_channels as channels
from app.config import settings
from app.security import encrypt_secret
from app.operations import create_notification, save_setting
from app.models import NotificationDelivery, ConversationState
from app.notification_dispatch import process_notification_delivery
from app.ops_schemas import NotificationSettings
from live_fixture import setup


def config(channel='email'):
    return {'enabled':True,'channel':channel,'recipients':{'12':'advisor@example.com' if channel=='email' else 'advisor12'},
        'encrypted_connection_secret':encrypt_secret('test-only').decode(),'smtp_host':'smtp.example.com','smtp_port':465,
        'smtp_user':'sender@example.com','sender_email':'sender@example.com','smtp_security':'ssl',
        'corp_id':'test-corp','wecom_agent_id':7}


def note(factory,monkeypatch,channel='email'):
    setup(factory,monkeypatch)
    with factory() as db:
        save_setting(db,'notification_settings',config(channel))
        n=create_notification(db,'assignment.routed','客户分配','场景：客户留资',26)
        n.target_agent_id=12
        db.add(NotificationDelivery(notification_id=n.id));db.commit()


def test_email_actual_owner_mapping_link_and_no_duplicate(session_factory,monkeypatch):
    note(session_factory,monkeypatch)
    sent=[]
    class SMTP:
        def __init__(self,*a,**kw):assert kw['context'] and kw['timeout']==10
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def login(self,user,password):assert password=='test-only'
        def send_message(self,message):sent.append(message);return {}
    monkeypatch.setattr(channels.smtplib,'SMTP_SSL',SMTP)
    assert process_notification_delivery(session_factory)
    assert not process_notification_delivery(session_factory)
    assert len(sent)==1 and sent[0]['To']=='advisor@example.com'
    assert '/conversations/26' in sent[0].get_content()
    with session_factory() as db:assert db.scalar(select(NotificationDelivery)).status=='delivered'


@pytest.mark.parametrize('after_submission,expected',[(False,'retry'),(True,'submission_unknown')])
def test_email_failure_does_not_blindly_repeat(session_factory,monkeypatch,after_submission,expected):
    note(session_factory,monkeypatch)
    def fail(config,payload,on_submit):
        if after_submission:on_submit()
        raise TimeoutError('network')
    monkeypatch.setattr(channels,'send_advisor_notification',fail)
    assert process_notification_delivery(session_factory)
    with session_factory() as db:assert db.scalar(select(NotificationDelivery)).status==expected
    assert not process_notification_delivery(session_factory)


def test_missing_mapping_fails_without_external_send(session_factory,monkeypatch):
    note(session_factory,monkeypatch)
    with session_factory() as db:
        db.get(ConversationState,1).assignee_id=99;db.commit()
    monkeypatch.setattr(channels.smtplib,'SMTP_SSL',lambda *a,**kw:pytest.fail('wrong recipient must not connect'))
    assert process_notification_delivery(session_factory)
    with session_factory() as db:
        delivery=db.scalar(select(NotificationDelivery))
        assert delivery.status=='dead' and delivery.error_code=='notification_advisor_recipient_missing'


def test_wecom_direct_member_and_receipt(session_factory,monkeypatch):
    note(session_factory,monkeypatch,'wecom')
    payloads=[]
    class Response:
        def __init__(self,data):self.data=data
        def raise_for_status(self):pass
        def json(self):return self.data
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,url,params):assert params['corpsecret']=='test-only';return Response({'access_token':'token'})
        def post(self,url,json):payloads.append(json);return Response({'errcode':0,'msgid':'one'})
    monkeypatch.setattr(channels.httpx,'Client',Client)
    assert process_notification_delivery(session_factory)
    assert payloads[0]['touser']=='advisor12' and payloads[0]['agentid']==7
    assert payloads[0]['enable_duplicate_check']==1
    assert not process_notification_delivery(session_factory)
    channels.send_advisor_notification(config('wecom'),
        {'target_agent_id':12,'title':'客户分配','body':'繁體顧問訊息'*500,'notification_id':999},lambda:None)
    assert len(payloads[-1]['text']['content'].encode('utf-8')) <= 1800
    assert '\ufffd' not in payloads[-1]['text']['content']


def test_wecom_secret_urls_redacted():
    record=logging.LogRecord('httpx',20,'test',1,'HTTP Request: %s %s',('GET','https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpsecret=secret'),None)
    assert channels._RedactWecomQueries().filter(record)
    assert 'secret' not in record.getMessage()


def test_evaluation_never_dispatches(session_factory,monkeypatch):
    note(session_factory,monkeypatch)
    monkeypatch.setattr(settings,'app_profile','evaluation')
    monkeypatch.setattr(channels,'send_advisor_notification',lambda *a:pytest.fail('evaluation dispatch'))
    assert not process_notification_delivery(session_factory)


@pytest.mark.parametrize('channel',['email','wecom'])
@pytest.mark.parametrize('blocked',['inbox_disabled','inbox_inactive','allowlist_empty','account_mismatch'])
def test_external_notification_rechecks_scope_before_send(session_factory,monkeypatch,channel,blocked):
    from app.models import ChatwootConnection
    note(session_factory,monkeypatch,channel)
    with session_factory() as db:
        conversation=db.get(ConversationState,1)
        if blocked=='inbox_disabled':conversation.inbox.ai_enabled=False
        elif blocked=='inbox_inactive':conversation.inbox.status='inactive'
        elif blocked=='allowlist_empty':save_setting(db,'route_reception_config',{'routing':{'live_inbox_ids':[]}})
        else:db.get(ChatwootConnection,1).account_id=999
        db.commit()
    monkeypatch.setattr(channels,'send_advisor_notification',lambda *a:pytest.fail('blocked scope must not send'))
    assert process_notification_delivery(session_factory)
    with session_factory() as db:
        delivery=db.scalar(select(NotificationDelivery))
        assert delivery.status=='cancelled' and delivery.error_code=='notification_scope_mismatch'


@pytest.mark.parametrize('recipients',[{0:'person@example.com'},{12:'a\r\nb@example.com'},{12:'not-an-email'}])
def test_invalid_email_mapping_rejected(recipients):
    with pytest.raises(ValueError):NotificationSettings(channel='email',recipients=recipients)


def test_notification_secret_is_write_only_and_bound_to_destination(authenticated):
    client,csrf=authenticated
    payload={'enabled':True,'channel':'email','recipients':{'12':'advisor@example.com'},
             'smtp_host':'smtp.example.com','smtp_user':'sender@example.com','sender_email':'sender@example.com',
             'connection_secret':'test-authorization-code'}
    headers={'X-CSRF-Token':csrf}
    response=client.patch('/v1/settings/notifications',json=payload,headers=headers)
    assert response.status_code==200
    assert response.json()['connection_secret_configured'] is True
    assert 'test-authorization-code' not in response.text
    assert 'test-authorization-code' not in client.get('/v1/settings/notifications').text
    payload.pop('connection_secret')
    assert client.patch('/v1/settings/notifications',json=payload,headers=headers).status_code==200
    payload['smtp_host']='different.example.com'
    assert client.patch('/v1/settings/notifications',json=payload,headers=headers).status_code==422
    payload['smtp_host']='smtp.example.com';payload['smtp_port']=587
    assert client.patch('/v1/settings/notifications',json=payload,headers=headers).status_code==422
    wecom={'enabled':True,'channel':'wecom','recipients':{'12':'advisor12'},'corp_id':'corp',
           'wecom_agent_id':7,'connection_secret':'test-application-secret'}
    assert client.patch('/v1/settings/notifications',json=wecom,headers=headers).status_code==200
    wecom.pop('connection_secret');wecom['wecom_agent_id']=8
    assert client.patch('/v1/settings/notifications',json=wecom,headers=headers).status_code==422
