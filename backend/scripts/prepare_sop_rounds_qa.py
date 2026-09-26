"""Seed an isolated UI test database; refuse to touch the application database."""
from pathlib import Path
from sqlalchemy import select
from app.config import settings
from app.db import Base, engine, SessionLocal
from app import automation_models
from app.models import Tenant, InboxBinding, Contact, ConversationState, MessageEvent, ChatwootLabel, utcnow

assert settings.database_url.endswith('/sop-rounds-ui.db')
assert settings.app_profile == 'evaluation' and not settings.outbound_enabled
Base.metadata.create_all(engine)
with SessionLocal() as db:
    if not db.get(Tenant, 1):
        db.add(Tenant(id=1, name='Local QA'))
        db.flush()
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name='QA Facebook', channel_type='Channel::FacebookPage', ai_enabled=True))
        db.add(Contact(id=1, tenant_id=1, chatwoot_contact_id=1, name='QA Customer'))
        db.add_all([ChatwootLabel(tenant_id=1,chatwoot_label_id=i,title=name) for i,name in enumerate(['qa-start','qa-stop','normal'],1)])
        db.flush()
        db.add(ConversationState(id=1, tenant_id=1, inbox_binding_id=1, contact_id=1, chatwoot_conversation_id=26, can_reply=True))
        db.flush()
        db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=1, direction='incoming', content='QA customer message', created_at=utcnow()))
        db.commit()
exec(Path('scripts/prepare_ui_qa.py').read_text(encoding='utf8'))
