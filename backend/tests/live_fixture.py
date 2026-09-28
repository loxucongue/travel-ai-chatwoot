from datetime import timedelta
from app.config import settings
from app.models import ChatwootConnection, InboxBinding, Contact, ConversationState, MessageEvent, AppSetting, utcnow
from app.security import encrypt_secret
from app.runtime_settings import dt, iso


def setup(factory, monkeypatch):
    monkeypatch.setattr(settings, 'app_profile', 'live_reply')
    monkeypatch.setattr(settings, 'outbound_mode', 'live')
    monkeypatch.setattr(settings, 'chatwoot_write_enabled', True)
    monkeypatch.setattr(settings, 'live_sop_conversation_ids', '26')
    with factory() as db:
        db.add(ChatwootConnection(id=1, tenant_id=1, account_id=180474, encrypted_api_token=encrypt_secret('fake'), connection_key='fake'))
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name='test', channel_type='Channel::FacebookPage', ai_enabled=True))
        db.add(Contact(id=1, tenant_id=1, chatwoot_contact_id=55, name='Test'))
        db.flush()
        db.add(ConversationState(id=1, tenant_id=1, inbox_binding_id=1, contact_id=1, chatwoot_conversation_id=26,
            labels=['ai'], ai_mode='enabled', ai_label_present=True, can_reply=True, last_customer_message_at=utcnow()))
        db.flush()
        db.add(MessageEvent(id=1, conversation_state_id=1, chatwoot_message_id=100, direction='incoming', content='test'))
        db.add(AppSetting(key='live_reply', value={'armed_at':iso(dt(utcnow())-timedelta(seconds=30))}))
        db.add(AppSetting(key='global_message_sending', value={'enabled':True}))
        db.commit()
