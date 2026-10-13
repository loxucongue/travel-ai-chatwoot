"""Install the confirmed cadence and first-quote validity without sending messages."""
from copy import deepcopy
import json
from pathlib import Path
import sys


def update_package(package):
    result = deepcopy(package)
    if result['route_variant'] != 'peach_11d_2027':
        return result
    price = next(s for s in result['fixed_answers'] if s['id'] == 'price')
    price.update(valid_from=None, valid_until=None, valid_for_days=30,
                 usage_note='客户问费用才报价。首次报价起30天，同一客户截止日固定；过期使用标准价。')
    price['answer_text'] = price['answer_text'].replace('截至10/31前報名，國慶月優惠', '截至{offer_deadline}前報名，限時優惠')
    assert '{offer_deadline}' in price['answer_text'] and price.get('fallback_text')
    for key in ('price_reference', 'price_deferral'):
        if key in result['content_groups']:
            result['content_groups'][key]['approved_text'] = price['answer_text']
    return result


def update_config(config):
    result = deepcopy(config)
    result['silence'].update(initial_checkpoints_minutes=[10,25,40,60], recurring_interval_minutes=180,
        active_start='07:00', active_end='01:00', max_proactive_messages_per_day=12)
    return result


def migrate_sessions(db, config, packages, now):
    from sqlalchemy import select
    from sqlalchemy.dialects.sqlite import insert
    from app.automation_models import AutomationSession, CustomerQuoteOffer
    from app.reception_v3 import service
    from app.reception_v3.quotes import subject_key
    from app.models import ConversationState
    preserved, scheduled = 0, []
    for row in db.scalars(select(AutomationSession).where(AutomationSession.environment == 'live', AutomationSession.engine_version == 'v3')).all():
        # Honor dates explicitly promised before this release, across conversations.
        for route, package in packages.items():
            price = next((s for s in package['fixed_answers'] if s['id'] == 'price'), {})
            until = price.get('valid_until')
            if not until:
                continue
            date_text = f"{int(until[5:7])}/{int(until[8:10])}"
            for message in row.messages:
                text = message.get('content', '')
                if (message.get('direction') == 'outgoing' and message.get('status') in ('submitted','sent','delivered','read')
                        and f'截至{date_text}' in text and '12,180' in text):
                    promise = {'first_quoted_on': message['created_at'][:10], 'valid_until': until, 'source': 'existing_promise'}
                    result = db.execute(insert(CustomerQuoteOffer).values(subject_key=subject_key(db,row),route_variant=route,offer=promise)
                        .on_conflict_do_nothing(index_elements=['subject_key','route_variant']))
                    preserved += result.rowcount
                    break
        value = service.state(row)
        conversation = db.get(ConversationState, row.conversation_state_id)
        if (row.controls.get('simulation',{}).get('status') != 'running' or row.controls.get('human')
                or value.get('handoff') or value.get('opt_out') or row.memory.get('reception_status') == 'abusive'
                or (conversation and conversation.ai_mode != 'enabled') or value.get('appointment_pending')
                or value.get('delivery_kind') or value.get('pending_event') not in (None, 'silence_due')):
            continue
        incoming = [m for m in row.messages if m.get('direction') == 'incoming' and m.get('created_at')]
        if not incoming or service.date(now) > service.date(incoming[-1]['created_at']) + service.timedelta(hours=23,minutes=55):
            continue
        value['followup_entry_at'] = incoming[0]['created_at']
        value['followup_customer_replied'] = len(incoming) > 1
        replies = [m['created_at'] for m in row.messages if m.get('direction') == 'outgoing'
                   and not m.get('proactive') and m.get('status') in ('submitted','sent','delivered','read')]
        value['followup_anchor'] = max(replies) if value['followup_customer_replied'] and replies else incoming[0]['created_at']
        value.update(silence_step=0, pending_event=None, followup_settings=config['silence'])
        value['followup_intervals'] = service.followup_intervals(value,config['silence'])
        value['next_check_minutes'] = 180
        service.schedule_followup(value, config, now)
        service.save(row,value)
        row.due_at = None
        if value.get('next_check_at'):
            scheduled.append({'session_id':row.id,'next_check_at':value['next_check_at']})
    return {'preserved_promises':preserved,'rescheduled':scheduled}


def main():
    import argparse
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from app.route_packages import load_route_packages, install_route_package
    from app.db import SessionLocal, engine
    from app.automation_models import CustomerQuoteOffer, AutomationSession
    from app.operations import setting_value, save_setting
    from app.reception_config import ReceptionConfiguration
    from app.models import utcnow
    from sqlalchemy import select
    parser=argparse.ArgumentParser(); parser.add_argument('--apply',action='store_true'); parser.add_argument('--backup',type=Path,required=True)
    args=parser.parse_args()
    packages=load_route_packages()
    result={'applied':args.apply,'messages_sent':0}
    with SessionLocal() as db:
        before=setting_value(db,'route_reception_config',{})
        config=ReceptionConfiguration.model_validate(update_config(before)).model_dump()
        updated={key:update_package(value) for key,value in packages.items()}
        if args.apply:
            args.backup.mkdir(parents=True,exist_ok=False)
            backup={'config':before,'sessions': [{'id':r.id,'controls':r.controls,'due_at':r.due_at} for r in db.scalars(select(AutomationSession).where(AutomationSession.environment=='live'))]}
            (args.backup/'state.json').write_text(json.dumps(backup,ensure_ascii=False,indent=2),encoding='utf8')
            for key,package in packages.items():
                (args.backup/(key+'.json')).write_text(json.dumps(package,ensure_ascii=False,indent=2),encoding='utf8')
            CustomerQuoteOffer.__table__.create(engine,checkfirst=True)
            result.update(migrate_sessions(db,config,packages,utcnow()))
            for key,package in updated.items():
                if package != packages[key]:
                    package['package_version'] += '.offer30-20261013'
                    install_route_package(package)
            save_setting(db,'route_reception_config',config); db.commit()
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
