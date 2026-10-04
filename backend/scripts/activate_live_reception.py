"""Activate a verified Chatwoot inbox; run with workers stopped after release."""
import argparse
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inbox-id', type=int, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--silence-minutes', nargs='+', type=int, required=True)
    parser.add_argument('--since', required=True, help='UTC timestamp just before the release pause')
    args = parser.parse_args()
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.config import settings
    from app.models import ChatwootConnection, InboxBinding, utcnow
    from app.chatwoot import normalize_collection
    from app.chatwoot_service import client_for
    from app.operations import setting_value, save_setting, audit
    from app.reception_config import get_reception_configuration, ReceptionConfiguration, SETTING_KEY
    from app.outbound_control import global_message_sending_enabled
    from app.reception_v3.skills import compile_skills
    from app.reception_v3 import release_id
    from app.runtime_settings import dt, iso
    cutoff = iso(dt(args.since))
    assert dt(cutoff) <= dt(utcnow())
    backend = Path(__file__).resolve().parents[1]
    env_file = backend / '.env'
    assert settings.app_profile == 'live_reply' and settings.outbound_enabled
    with SessionLocal() as db:
        assert global_message_sending_enabled(db)
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.account_id == settings.live_reply_account_id))
        notification = setting_value(db, 'notification_settings', {})
        assert notification.get('enabled') and notification.get('channel') == 'chatwoot'
        client = client_for(connection)
        try:
            inbox = next(i for i in normalize_collection(client.list_inboxes()) if i['id'] == args.inbox_id)
            assert inbox['channel_type'] == 'Channel::FacebookPage'
            assert any(a['id'] == notification['agent_id'] for a in normalize_collection(client.list_inbox_agents(args.inbox_id)))
            assert any(b['id'] == notification['bot_id'] for b in normalize_collection(client.request('GET', 'agent_bots')))
        finally:
            client.close()
        from scripts.update_business_wording import corrected_configuration
        config = get_reception_configuration(db)
        before_config = json.loads(json.dumps(config))
        config = corrected_configuration(config)
        config['silence'].update(enabled=True, live_enabled=True, intervals_minutes=args.silence_minutes)
        config = ReceptionConfiguration.model_validate(config).model_dump()
        args.backup.mkdir(parents=True, exist_ok=False)
        args.backup.chmod(0o700)
        shutil.copy2(env_file, args.backup / '.env')
        with sqlite3.connect(backend / 'data/app.db') as src, sqlite3.connect(args.backup / 'app.db') as dst:
            src.backup(dst)
        before = {'reception': before_config, 'notification': notification,
                  'live_reply': setting_value(db, 'live_reply', {}), 'inbox_id': settings.live_reply_inbox_id}
        (args.backup / 'settings.json').write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf8')
        binding = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == connection.tenant_id,
            InboxBinding.chatwoot_inbox_id == args.inbox_id))
        if binding is None:
            binding = InboxBinding(tenant_id=connection.tenant_id, chatwoot_inbox_id=args.inbox_id)
            db.add(binding)
        binding.name = inbox['name']; binding.channel_type = inbox['channel_type']
        binding.ai_enabled = True; binding.status = 'active'; binding.last_synced_at = utcnow()
        old = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == connection.tenant_id,
            InboxBinding.chatwoot_inbox_id == settings.live_reply_inbox_id))
        if old and old is not binding:
            old.ai_enabled = False; old.status = 'inactive'
        policy = setting_value(db, 'live_reply', {})
        # Activation does not replay, resume or retime any historical session.
        policy['auto_new_customers_since'] = cutoff
        save_setting(db, 'live_reply', policy)
        save_setting(db, SETTING_KEY, config)
        notification = {**notification, 'assign_on_handoff': True}
        save_setting(db, 'notification_settings', notification)
        text = env_file.read_text(encoding='utf-8-sig')
        line = 'LIVE_REPLY_INBOX_ID=' + str(args.inbox_id)
        text = re.sub(r'^LIVE_REPLY_INBOX_ID=.*$', line, text, flags=re.M) if re.search(r'^LIVE_REPLY_INBOX_ID=', text, re.M) else text + '\n' + line + '\n'
        audit(db, None, 'live.activate', 'inbox', args.inbox_id, {'since': policy['auto_new_customers_since'], 'silence_minutes': args.silence_minutes})
        env_file.write_text(text, encoding='utf8')
        try:
            db.commit()
        except Exception:
            shutil.copy2(args.backup / '.env', env_file)
            raise
        bundle = compile_skills(db)
        print(json.dumps({'release': release_id(), 'inbox_id': args.inbox_id, 'binding_id': binding.id,
            'since': policy['auto_new_customers_since'], 'silence': bundle['silence'],
            'agent_id': notification['agent_id'], 'bot_id': notification['bot_id'],
            'opening_unchanged': config['reply'] == before_config['reply'], 'backup': str(args.backup)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
