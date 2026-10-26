"""Initialize an empty company after Alembic migrations; never resets existing data."""
import json
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.models import Tenant, User
from app.operations import save_setting
from app.reception_config import default_reception_configuration, SETTING_KEY
from app.security import hash_password


def main():
    if settings.business_profile != 'rujing2' or settings.outbound_enabled:
        raise SystemExit('Bootstrap requires rujing2 with outbound disabled')
    destination = Path(sys.argv[1])
    if destination.exists():
        raise SystemExit('Credential file already exists')
    with SessionLocal() as db:
        if db.scalar(select(Tenant)) or db.scalar(select(User)):
            raise SystemExit('Database is not empty; refusing to reset company data')
        credentials = {'email': 'admin@rujing2.example.com', 'password': secrets.token_urlsafe(24)}
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(credentials, stream, indent=2)
        db.add(Tenant(name=settings.company_name, ai_enabled=False))
        db.add(User(email=credentials['email'], display_name='Administrator',
                    password_hash=hash_password(credentials['password']), role='super_admin',
                    must_change_password=True))
        save_setting(db, SETTING_KEY, default_reception_configuration())
        save_setting(db, 'global_message_sending', {'enabled': False})
        db.commit()
    print('Initialized company, empty configuration and first-login administrator')


if __name__ == '__main__':
    main()
