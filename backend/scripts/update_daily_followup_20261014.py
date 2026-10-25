"""Migrate cadence without replaying customer messages or overriding human control."""
from copy import deepcopy
from collections import Counter
import json
from pathlib import Path
import sys


def update_config(config):
    result = deepcopy(config)
    result['silence'].update(daily_at='09:00', initial_checkpoints_minutes=None,
                            max_proactive_messages_per_day=1)
    from app.reception_config import ReceptionConfiguration
    ReceptionConfiguration.model_validate(result)
    return result


def replan(row, bundle, now, *, blocked_reason=None, customer_at=None):
    from app.reception_v3 import service
    value = service.state(row)
    if blocked_reason or row.controls.get('human') or value.get('handoff') or value.get('opt_out'):
        return blocked_reason or 'human_or_optout'
    if row.controls.get('simulation', {}).get('status') != 'running':
        return 'stopped_session'
    if value.get('appointment_pending'):
        return 'explicit_appointment_preserved'
    value['followup_settings'] = deepcopy(bundle['silence'])
    if (value.get('pending_event') not in (None, 'silence_due')
            or value.get('intake_status') in ('sending', 'waiting')
            or any(m.get('status') == 'draft' and not m.get('proactive') for m in row.messages)):
        service.save(row, value)
        return 'active_reception_preserved'
    incoming = [service.date(m['created_at']) for m in row.messages
                if m.get('direction') == 'incoming' and m.get('created_at')]
    if customer_at:
        incoming.append(service.date(customer_at))
    if not incoming:
        return 'no_customer_timestamp'
    customer = max(incoming)
    replies = [service.date(m['created_at']) for m in row.messages
               if m.get('direction') == 'outgoing' and not m.get('proactive')
               and m.get('status') in ('simulated_delivered', 'submitted', 'sent', 'delivered', 'read')
               and m.get('created_at')]
    proactive = [service.date(m['created_at']) for m in row.messages if m.get('proactive')
                 and m.get('status') in ('simulated_delivered', 'submitted', 'sent', 'delivered', 'read')
                 and m.get('created_at')]
    value.update(followup_last_customer_at=customer.isoformat(),
                 followup_anchor=max([customer, *replies]).isoformat(),
                 daily_last_touch_at=max(proactive).isoformat() if proactive else None,
                 next_check_minutes=1440, silence_step=0, pending_event=None,
                 followup_migration='daily-20261014')
    cancelled_plans = {m.get('plan_id') for m in row.messages if m.get('proactive') and m.get('status') == 'draft'}
    row.messages = [{**m, 'status': 'cancelled'} if m.get('proactive') and m.get('status') == 'draft' else m for m in row.messages]
    if value.get('delivery_plan_id') in cancelled_plans:
        value.update(delivery_kind=None, delivery_due_at=None)
    service.schedule_followup(value, bundle, now)
    service.save(row, value)
    row.generation += 1
    row.due_at = None
    return 'scheduled' if value['next_check_at'] else 'outside_channel_window'


def main():
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.automation_models import AutomationSession
    from app.models import ConversationState, utcnow
    from app.operations import setting_value, save_setting
    from app.reception_v3 import live
    from app.history_sync import message_timestamp
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    now = utcnow()
    records = []
    with SessionLocal() as db:
        before = setting_value(db, 'route_reception_config', {})
        bundle = update_config(before)
        snapshots = []
        for row in db.scalars(select(AutomationSession).where(AutomationSession.engine_version == 'v3', AutomationSession.environment == 'live')):
            conversation = db.get(ConversationState, row.conversation_state_id)
            snapshots.append({'id':row.id, 'generation':row.generation, 'controls':deepcopy(row.controls),
                              'messages':deepcopy(row.messages), 'due_at':row.due_at})
            reason = None if conversation and live.permitted(db, conversation) else 'reception_not_permitted'
            if row.memory.get('reception_status') == 'abusive':
                reason = 'abusive'
            result = replan(row, bundle, now, blocked_reason=reason,
                            customer_at=message_timestamp(conversation.last_customer_message_at) if conversation and conversation.last_customer_message_at else None)
            records.append({'session_id':row.id, 'conversation_id':conversation.chatwoot_conversation_id if conversation else None,
                            'result':result, 'next_check_at':(row.controls.get('v3') or {}).get('next_check_at')})
        if args.apply:
            args.backup.mkdir(parents=True, exist_ok=False)
            (args.backup/'before.json').write_text(json.dumps({'config':before,'sessions':snapshots}, ensure_ascii=False,indent=2),encoding='utf8')
            save_setting(db, 'route_reception_config', bundle)
            db.commit()
        else:
            db.rollback()
    result = {'applied':args.apply,'now':now,'daily_at':'09:00','first_hour_enabled':False,
              'counts':dict(Counter(r['result'] for r in records)),'sessions':records,'test_messages_sent':0}
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    main()
