"""Delay pending ordinary follow-ups; never resume stopped or historical sessions."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


def updated(config):
    value = deepcopy(config)
    value['silence']['intervals_minutes'] = [360]
    return value


def reschedule(controls, messages, due_at, now):
    from app.reception_v3.service import date, later
    result = deepcopy(controls)
    state = result.get('v3', {})
    if (result.get('simulation', {}).get('status') != 'running' or result.get('human')
            or state.get('handoff') or state.get('opt_out') or state.get('appointment_pending')
            or state.get('followup_intervals') == [360] or not state.get('followup_anchor')):
        return controls, messages, due_at
    deadline = later(state['followup_anchor'], 360 * 60)
    state.update(followup_intervals=[360], followup_until=deadline, silence_step=0,
                 next_check_minutes=360, next_check_at=deadline if date(deadline)>date(now) else None)
    if state.get('pending_event') == 'silence_due':
        state['pending_event'] = None
        due_at = None
    # A generated but unsent proactive message must not leak out on the old schedule.
    messages = [dict(m, status='cancelled') if m.get('proactive') and m.get('status')=='draft' else m for m in messages]
    if not any(m.get('status')=='draft' for m in messages) and state.get('delivery_kind')=='reply':
        state.update(delivery_kind=None, delivery_due_at=None)
    result['v3'] = state
    return result, messages, due_at


def main():
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.automation_models import AutomationSession
    from app.operations import save_setting
    from app.reception_config import SETTING_KEY, ReceptionConfiguration, get_reception_configuration
    from app.reception_v3.service import date, later
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path, required=True)
    args=parser.parse_args()
    now=datetime.now(timezone.utc).isoformat()
    with SessionLocal() as db:
        before=get_reception_configuration(db)
        after=ReceptionConfiguration.model_validate(updated(before)).model_dump()
        changes=[]
        for row in db.scalars(select(AutomationSession).where(AutomationSession.engine_version=='v3')):
            last=row.controls.get('simulation', {}).get('last_wall_at', now)
            virtual_now=later(row.virtual_now, max(0,(date(now)-date(last)).total_seconds()))
            revised=reschedule(row.controls, row.messages, row.due_at, virtual_now)
            if revised == (row.controls, row.messages, row.due_at):continue
            changes.append({'id':row.id, 'controls':row.controls, 'messages':row.messages, 'due_at':row.due_at})
            if args.apply:row.controls,row.messages,row.due_at=revised
        if args.apply:
            args.backup.parent.mkdir(parents=True, exist_ok=True)
            with args.backup.open('x',encoding='utf8') as f:
                json.dump({'before':before,'after':after,'sessions':changes},f,ensure_ascii=False)
            save_setting(db,SETTING_KEY,after)
            db.commit()
        print(json.dumps({'applied':args.apply,'sessions_rescheduled':len(changes),
                          'intervals_minutes':after['silence']['intervals_minutes'],'history_replayed':False}))


if __name__=='__main__':main()
