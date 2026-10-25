from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import ConversationState, ConversationLabelSync, HandoffTask, MessageEvent, OutboundMessage, utcnow
from app.reception_v3 import service, live
from app.reception_v3.followup_window import within_window
from app.runtime_settings import dt
from live_fixture import setup
from scripts.update_daily_followup_20261014 import replan
from test_daily_followup_20261014 import spec, migration_row
from test_live_audit_fixes import session, event
from test_reception_v3 import create, answer, step, delivered
from test_conversation_controls import FakeChatwoot


@pytest.mark.parametrize('age,allowed', [
    (timedelta(hours=25), True), (timedelta(days=6, hours=23, minutes=59), True),
    (timedelta(days=7), False), (timedelta(days=8), False),
])
def test_seven_day_exact_boundary(age, allowed):
    now = '2026-10-20T20:00:00+08:00'
    assert within_window((dt(now) - age).isoformat(), now) is allowed


def test_seven_daily_touches_then_stop_without_customer_reply(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        monkeypatch.setattr(service, 'compile_skills', lambda db: spec())
        calls = []
        monkeypatch.setattr(service, 'run_agent', lambda c: (calls.append(c) or answer(messages=[{'text': '一則跟進'}])))
        row.virtual_now = '2026-10-18T20:00:00+08:00'
        row.messages = [{'id':'customer', 'direction':'incoming', 'content':'先看看', 'created_at':row.virtual_now}]
        value = {'opening_started': True, 'followup_last_customer_at':row.virtual_now, 'followup_settings':spec()['silence']}
        service.schedule_followup(value, spec(), row.virtual_now, reset=True)
        service.save(row, value); db.commit()
        for day in range(19, 26):
            assert dt(service.state(row)['next_check_at']).day == day
            assert dt(service.state(row)['next_check_at']).astimezone(ZoneInfo('Asia/Taipei')).hour == 9
            service.advance_next(row); db.commit(); step(db, row, 0); step(db, row, 2)
        assert len(calls) == len(delivered(row)) == 7
        assert service.state(row)['next_check_at'] is None
        assert dt(service.state(row)['followup_until']) == dt('2026-10-25T20:00:00+08:00')


def test_customer_reply_cancels_old_draft_and_starts_new_seven_days(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.virtual_now = '2026-10-23T15:00:00+08:00'
        row.messages = [{'id':'old','direction':'incoming','created_at':'2026-10-18T20:00:00+08:00'},
                        {'id':'draft','direction':'outgoing','proactive':True,'status':'draft','plan_id':'old'}]
        service.save(row, {'opening_started':True,'delivery_kind':'reply','delivery_plan_id':'old'})
        service.add_message(db, row, '我們兩位', 'new')
        value = service.state(row)
        assert row.messages[1]['status'] == 'cancelled'
        assert value['pending_event'] == 'customer_message'
        assert value['next_check_at'] is None
        service.schedule_followup(value, spec(), row.virtual_now, reset=True)
        assert dt(value['followup_until']) == dt('2026-10-30T15:00:00+08:00')
        assert dt(value['next_check_at']) == dt('2026-10-24T09:00:00+08:00')


def test_old_two_day_session_rearmed_without_replaying_or_changing_customer_time():
    row = migration_row()
    before = deepcopy(row.messages)
    row.controls['v3']['followup_skip_reason'] = 'outside_channel_window'
    assert replan(row, spec(), '2026-10-16T20:00:00+08:00') == 'scheduled'
    assert row.messages == before
    assert service.state(row)['next_check_at'] == '2026-10-17T09:00:00+08:00'
    assert not service.state(row).get('followup_skip_reason')


@pytest.mark.parametrize('age', [2, 6])
def test_manual_ai_tag_answers_old_unanswered_question_without_old_human_tag_return(session_factory, monkeypatch, age):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db)
        event(db, 'conversation_updated', ['人工接管'], 10)
        message = db.get(MessageEvent, 1)
        message.created_at = (dt(utcnow()) - timedelta(days=age)).isoformat()
        db.get(ConversationState, 1).last_customer_message_at = message.created_at
        db.commit()
        event(db, 'conversation_updated', [], 11)
        event(db, 'conversation_updated', ['ai'], 12)
        assert service.state(row)['pending_event'] == 'customer_message'
        assert dt(row.due_at) <= dt(utcnow())
        assert not row.controls['human']
        assert all(t.status == 'cancelled' for t in db.scalars(select(HandoffTask)))
        generation = row.generation
        event(db, 'conversation_updated', ['人工接管'], 9)  # Delayed old webhook.
        assert row.generation == generation
        assert db.get(ConversationState, 1).labels == ['ai']
        assert db.get(ConversationLabelSync, 1).target == 'ai'


@pytest.mark.parametrize('can_reply,expected', [(True, 1), (False, 0)])
def test_day_three_delivery_obeys_actual_channel_availability(session_factory, monkeypatch, can_reply, expected):
    setup(session_factory, monkeypatch)
    fake = FakeChatwoot(); fake.can_reply = can_reply
    sent = []
    monkeypatch.setattr(fake, 'create_text_message', lambda cid, text: (sent.append(text) or {'id':777,'status':'sent'}), raising=False)
    monkeypatch.setattr(live, 'client_for', lambda _: fake)
    with session_factory() as db:
        row = session(db); row.messages = []
        db.get(ConversationState, 1).last_customer_message_at = (dt(utcnow()) - timedelta(days=3)).isoformat()
        value = service.state(row); value['pending_event'] = None
        service.append_plan(row, value, [{'content':'跟進', 'content_type':'text', 'proactive':True}], 'reply', 1, 999)
        service.save(row, value); db.commit()
        live.deliver_one(db, row)
        assert len(sent) == expected
        assert len(list(db.scalars(select(OutboundMessage)))) == expected


def test_manual_return_of_answered_thread_schedules_daily_without_reanswer(session_factory, monkeypatch):
    from test_human_return_20261016 import prepared
    prepared(session_factory, monkeypatch)
    monkeypatch.setattr('app.reception_v3.skills.compile_skills', lambda db:spec())
    with session_factory() as db:
        event(db, 'conversation_updated', ['人工接管'], 10)
        event(db, 'conversation_updated', ['ai'], 12)
        value = service.state(live.session_for(db, 1))
        assert not value['pending_event']
        assert dt(value['next_check_at']).astimezone(ZoneInfo('Asia/Taipei')).hour == 9
        assert not db.scalar(select(OutboundMessage.id))


@pytest.mark.parametrize('patch,reason', [
    ({'human':True}, 'human_control_preserved'),
    ({'human_followup':{'enabled':True}}, 'human_control_preserved'),
    ({'v3':{'opt_out':True}}, 'optout_preserved'),
    ({'v3':{'failed_event':'customer_message'}}, 'failed_or_busy'),
    ({'v3':{'pending_event':'customer_message'}}, 'active_reception_preserved'),
    ({'v3':{'seven_day_migration':'20261020'}}, 'already_applied'),
    ({}, None),
])
def test_backfill_preserves_control_and_errors_but_not_finished_lease_marker(patch, reason):
    from scripts.migrate_seven_day_followup_20261020 import eligible
    row = migration_row()
    row.memory = {}
    row.controls = {'simulation':{'status':'running'}, 'v3_lease':'historical-completed-run', 'v3':{}, **patch}
    conv = SimpleNamespace(last_customer_message_at='2026-10-18T20:00:00+08:00')
    assert eligible(row, conv, '2026-10-20T20:00:00+08:00') == reason
