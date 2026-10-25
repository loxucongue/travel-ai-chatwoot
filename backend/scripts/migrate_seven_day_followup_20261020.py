"""Rearm eligible old AI sessions without replaying messages or changing labels."""
from copy import deepcopy
from collections import Counter
import json
from pathlib import Path
import sys


def eligible(row, conversation, now):
    from app.reception_v3 import service
    from app.reception_v3.followup_window import within_window
    value = service.state(row)
    if value.get('seven_day_migration') == '20261020':
        return 'already_applied'
    if row.controls.get('human') or row.controls.get('human_followup', {}).get('enabled') or value.get('handoff'):
        return 'human_control_preserved'
    if value.get('opt_out') or row.memory.get('reception_status') == 'abusive':
        return 'optout_preserved'
    if row.controls.get('simulation', {}).get('status') != 'running':
        return 'stopped_session'
    if not within_window(conversation.last_customer_message_at, now):
        return 'past_seven_days'
    if value.get('failed_event') or value.get('retry_at'):
        return 'failed_or_busy'
    if (value.get('pending_event') or value.get('appointment_pending')
            or value.get('intake_status') in ('sending', 'waiting')
            or any(m.get('status') in ('draft', 'submitting') for m in row.messages)):
        return 'active_reception_preserved'
    return None


def main():
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sqlalchemy import select
    from sqlalchemy.orm.exc import StaleDataError
    from app.db import SessionLocal
    from app.models import ConversationState, ChatwootConnection, utcnow
    from app.automation_models import AutomationSession, AutomationRun
    from app.reception_v3 import service, live
    from app.reception_v3.skills import compile_skills
    from app.conversation_labels import label_keys
    from app.chatwoot_service import client_for, payload_dict
    from app.operations import audit
    from scripts.update_daily_followup_20261014 import replan
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    now, records = utcnow(), []
    with SessionLocal() as db:
        ids = list(db.scalars(select(AutomationSession.id).where(
            AutomationSession.engine_version == 'v3', AutomationSession.environment == 'live')))
    for sid in ids:
        with SessionLocal() as db:
            row = db.get(AutomationSession, sid)
            conv = db.get(ConversationState, row.conversation_state_id)
            item = {'session_id':sid, 'conversation_id':conv.chatwoot_conversation_id}
            reason = eligible(row, conv, now)
            if not reason and db.scalar(select(AutomationRun.id).where(
                    AutomationRun.session_id == sid, AutomationRun.status == 'processing')):
                reason = 'active_model_preserved'
            if not reason and not live.permitted(db, conv):
                reason = 'reception_not_permitted'
            if reason:
                records.append({**item, 'result':reason})
                continue
            conn = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conv.tenant_id))
            api = client_for(conn)
            try:
                current = payload_dict(api.get_conversation(conv.chatwoot_conversation_id))
            finally:
                api.close()
            if current.get('can_reply') is False or label_keys(current.get('labels') or []) != label_keys(conv.labels or []):
                records.append({**item, 'result':'remote_control_changed'})
                continue
            before = {'controls':deepcopy(row.controls), 'generation':row.generation, 'due_at':row.due_at,
                      'revision':row.revision, 'customer_at':conv.last_customer_message_at, 'labels':conv.labels}
            result = replan(row, compile_skills(db), now, customer_at=conv.last_customer_message_at)
            value = service.state(row)
            value['seven_day_migration'] = '20261020'
            service.save(row, value)
            item.update(result=result, next_check_at=value.get('next_check_at'), until=value.get('followup_until'))
            if args.apply:
                (args.evidence / f'session-{sid}-before.json').write_text(json.dumps(before, ensure_ascii=False), encoding='utf8')
                try:
                    audit(db,None,'conversation.seven_day_followup','conversation',conv.chatwoot_conversation_id,
                          {'next_check_at':item['next_check_at'],'until':item['until']})
                    db.commit()
                except StaleDataError:
                    db.rollback()
                    item['result'] = 'concurrent_change_preserved'
            else:
                db.rollback()
            records.append(item)
    print(json.dumps({'applied':args.apply, 'at':now, 'counts':dict(Counter(r['result'] for r in records)),
                      'sessions':records, 'customer_messages_sent_by_migration':0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
