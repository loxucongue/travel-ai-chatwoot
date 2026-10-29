"""Skipping a repetitive touch must not opt out or lose the next customer reply."""
from copy import deepcopy
import json

from app.reception_v3 import runtime, service
from test_daily_followup_20261014 import spec
from test_v3_intake_followup import create
from test_reception_v3 import answer, step, delivered
from test_reception_v3_skills import context, model, final


def test_silent_days_preserve_window_then_customer_can_resume(session_factory, monkeypatch):
    events = []
    def decision(c):
        events.append(c['event'])
        return (answer(action='wait', reason='No new relevant information')
                if c['event'] == 'silence_due' else
                answer(messages=[{'text': '回覆本次的新問題。'}]))
    with session_factory() as db:
        row = create(db, monkeypatch, entry='先看看，謝謝', model=decision)
        monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(spec()))
        row.virtual_now = '2026-10-26T19:00:00+08:00'
        row.messages = [{'id':'in', 'direction':'incoming', 'content':'先看看，謝謝',
                         'created_at':row.virtual_now}]
        value = {'opening_started':True, 'followup_entry_at':row.virtual_now,
                 'followup_last_customer_at':row.virtual_now, 'followup_settings':spec()['silence']}
        service.schedule_followup(value, spec(), row.virtual_now, reset=True)
        window = value['followup_until']
        service.save(row, value); db.commit()
        for day in (27, 28):
            service.advance_next(row); db.commit(); step(db, row, 0)
            state = service.state(row)
            assert not delivered(row)
            assert state['followup_until'] == window
            assert not state.get('opt_out') and not state.get('handoff')
            assert service.date(state['next_check_at']).day == day + 1
        service.add_message(db, row, '現在想問住宿', 'return'); db.commit()
        step(db, row, 0); step(db, row, 2)
        assert delivered(row) == ['回覆本次的新問題。']
        assert events == ['silence_due', 'silence_due', 'customer_message']


def test_runtime_exposes_history_without_forcing_followup_sales(monkeypatch):
    c = context()
    c.update(profile={}, state={}, event='silence_due')
    c['messages'] = [
        {'direction':'incoming', 'content':'我先看看謝謝'},
        {'direction':'outgoing', 'content':'好呀，您先看看。', 'status':'sent'},
        {'direction':'outgoing', 'content':'NEVER_SENT', 'status':'cancelled'},
    ]
    requests = model(monkeypatch, [final(action='wait')])
    result, _, _ = runtime.run_agent(c)
    assert result['action'] == 'wait' and not result['messages']
    payload = json.dumps(requests[0], ensure_ascii=False)
    assert '我先看看謝謝' in payload and '好呀，您先看看。' in payload
    assert '任务类型不代表必须发送或追加问题' in payload
    task = json.loads(requests[0]['messages'][-1]['content'])
    assert task['conversation_focus'] == {
        'latest_customer_statement':'我先看看謝謝',
        'recent_team_statements':['好呀，您先看看。'],
    }


def test_recency_view_is_bounded_without_removing_full_conversation(monkeypatch):
    c = context()
    c.update(profile={}, state={})
    c['messages'] = [
        {'direction':'outgoing', 'content':str(i) * 1800, 'status':'sent'}
        for i in range(7)] + [{'direction':'incoming','content':'請再說一次'}]
    requests = model(monkeypatch, [final(messages=[{'text':'重新說明。'}])])
    runtime.run_agent(c)
    task = json.loads(requests[0]['messages'][-1]['content'])
    assert task['conversation_focus']['recent_team_statements'] == [str(i)*1200 for i in range(3,7)]
    assert any(m.get('content') == '0'*1800 for m in requests[0]['messages'])


def test_advisor_followup_can_wait_without_changing_customer_profile(monkeypatch):
    requests = model(monkeypatch, [final(action='wait')])
    decision, _, _ = runtime.run_agent({'state':{'human_followup':True}, 'messages':[
        {'direction':'incoming', 'content':'先看看謝謝'},
        {'direction':'outgoing', 'content':'好呀，您先看看。', 'status':'sent'},
    ]})
    assert decision['action'] == 'wait' and not decision['messages']
    assert not decision['profile'] and decision['opt_out'] is None
    assert '只能重述旧结论' in requests[0]['messages'][0]['content']
