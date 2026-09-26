"""Create a separate UI-test database. It contains no real customer history or tokens."""
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.db import Base, engine, SessionLocal
from app.models import Tenant, User, InboxBinding, Contact, ConversationState, MessageEvent
from app.security import hash_password
from setup_material_pilot import seed

if "material-qa" not in settings.database_url:
    raise RuntimeError("isolated_qa_database_required")
Base.metadata.create_all(engine)
password=secrets.token_urlsafe(24)
with SessionLocal() as db:
    db.add(Tenant(id=1,name="Material QA"))
    db.add(User(id=1,email="material-qa@example.com",display_name="QA",password_hash=hash_password(password),role="admin"))
    db.flush()
    db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=128859,name="QA Facebook",channel_type="Channel::FacebookPage",ai_enabled=True))
    db.flush()
    db.add(Contact(id=1,tenant_id=1,chatwoot_contact_id=1,name="QA Test Customer"))
    db.flush()
    db.add(ConversationState(id=1,tenant_id=1,chatwoot_conversation_id=26,inbox_binding_id=1,contact_id=1,can_reply=True,labels=[]))
    db.flush()
    db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=1,direction="incoming",content="Test question",created_at="2026-08-26T02:00:00+00:00"))
    db.commit()
seed()
output=Path(__file__).resolve().parents[2]/".runlogs/material-qa.json"
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps({"email":"material-qa@example.com","password":password,"database_url":settings.database_url}),encoding="utf-8")
print("Isolated material QA database prepared")
