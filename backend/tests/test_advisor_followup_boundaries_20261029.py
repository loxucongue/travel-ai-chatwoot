"""Advisor answers are history, not new customer questions at timer expiry."""
import json
import time

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import ConversationState, ConversationLabelSync, OutboundMessage
from app.reception_v3 import runtime, repetition, service, live
from test_auto_advisor_followup_20261017 import enable
from test_human_return_20261016 import sync
from test_live_audit_fixes import event


BAD_REPLY = '三月底出發的話，價格跟剛剛這個差不多～您們時間上比較想偏三月底，還是四月初看桃花呢？'


def history():
    return [
        {'direction': 'incoming', 'content': '那三月下旬的價格呢？', 'status': 'received', 'created_at': '2026-10-29T03:34:19Z'},
        {'direction': 'outgoing', 'attribution': 'inferred_human', 'content': '稍等我給您報價', 'status': 'submitted', 'created_at': '2026-10-29T03:34:45Z'},
        {'direction': 'outgoing', 'attribution': 'inferred_human', 'content': '包團優惠價：¥11,180/人', 'status': 'submitted', 'created_at': '2026-10-29T03:37:48Z'},
    ]


def response(data):
    return httpx.Response(200, request=httpx.Request('POST', 'https://test.invalid'), json={
        'choices': [{'message': {'content': json.dumps(data, ensure_ascii=False)}}]})


def candidate(monkeypatch, text=BAD_REPLY):
    requests = []
    def post(payload, timeout):
        requests.append(payload)
        return response({'action': 'reply', 'messages': [{'text': text}]})
    monkeypatch.setattr(runtime, '_post_with_deadline', post)
    return requests


def context():
    return {'event': 'silence_due', 'now': '2026-10-29T04:07:49Z',
            'state': {'human_followup': True}, 'messages': history()}


def test_replay_493_followup_roles_and_reprice_suppression(monkeypatch):
    requests = candidate(monkeypatch)
    reviews = []
    def review(payload, timeout):
        reviews.append(payload)
        return response({'remove_clause_ids': [0], 'reason': '顾问已回答，不能重答或比较价格'})
    monkeypatch.setattr(repetition, '_post_with_deadline', review)
    monkeypatch.setattr(settings, 'reception_repeat_review_enabled', False)
    c = context()
    c['messages'] += [{'direction': 'outgoing', 'content': 'UNSENT', 'status': 'cancelled'},
                      {'direction': 'outgoing', 'content': 'PRIVATE', 'private': True, 'status': 'sent'}]
    decision, logs, _ = runtime.run_agent(c)
    assert decision['action'] == 'wait' and not decision['messages']
    task = json.loads(requests[0]['messages'][-1]['content'])
    assert [m['speaker'] for m in task['history']] == ['customer', 'advisor', 'advisor']
    assert task['customer_has_new_message'] is False
    assert 'UNSENT' not in str(requests + reviews) and 'PRIVATE' not in str(requests + reviews)
    assert any(log.get('removed_clauses') for log in logs)


@pytest.mark.parametrize('failure', ['timeout', 'invalid_ids', 'malformed'])
def test_followup_review_failure_waits_without_replaying_or_escalating(monkeypatch, failure):
    candidate(monkeypatch)
    def review(*args):
        if failure == 'timeout':
            raise TimeoutError('review_timeout')
        return response({'remove_clause_ids': [99]} if failure == 'invalid_ids' else {})
    monkeypatch.setattr(repetition, '_post_with_deadline', review)
    decision, logs, _ = runtime.run_agent(context())
    assert decision['action'] == 'wait' and not decision['messages']
    assert not decision['profile'] and not decision['route_variant']
    assert logs[-1]['status'] == 'failed'


def test_followup_review_deadline_does_not_send_unreviewed_text():
    decision = runtime.Decision(messages=[{'text': BAD_REPLY}]).model_dump()
    result, logs, _ = repetition.review(decision, context(), time.monotonic())
    assert result['action'] == 'wait' and logs[0]['error_code'] == 'deadline'


def test_novel_followup_allowed_then_repeated_question_is_suppressed(monkeypatch):
    text = '您和家人討論得如何呢？'
    candidate(monkeypatch, text)
    responses = [[], [0]]
    monkeypatch.setattr(repetition, '_post_with_deadline', lambda *a: response({'remove_clause_ids': responses.pop(0)}))
    c = context()
    first, _, _ = runtime.run_agent(c)
    assert first['messages'][0]['text'] == text
    c['messages'].append({'direction': 'outgoing', 'content': text, 'status': 'sent', 'attribution': 'ai'})
    second, _, _ = runtime.run_agent(c)
    assert second['action'] == 'wait' and not second['messages']


def test_timer_review_wait_has_no_outbound_and_customer_returns_to_advisor(session_factory, monkeypatch):
    from copy import deepcopy
    from test_reception_v3 import bundle
    enable(session_factory, monkeypatch)
    rules = bundle()
    rules['silence'].update(live_enabled=True, daily_at='09:00', active_start='07:00', active_end='01:00', max_proactive_messages_per_day=1)
    monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(rules))
    monkeypatch.setattr(service, 'candidate_materials', lambda *a: [])
    monkeypatch.setattr(service, 'active_web_facts', lambda *a, **k: ([], 'test'))
    monkeypatch.setattr(service, 'run_agent', runtime.run_agent)
    candidate(monkeypatch)
    monkeypatch.setattr(repetition, '_post_with_deadline', lambda *a: response({'remove_clause_ids': [0]}))
    with session_factory() as db:
        event(db, 'message_created', ['ai'], 30, 102, 'outgoing')
    fake = sync(session_factory, monkeypatch)
    fake.create_text_message = lambda *a: pytest.fail('reviewed wait must never send')
    monkeypatch.setattr(live, 'client_for', lambda *a: fake)
    with session_factory() as db:
        row = live.session_for(db, 1)
        row.virtual_now = service.state(row)['next_check_at']
        wall = row.controls['simulation']['last_wall_at']
        service.tick(db, session_id=row.id, wall_now=wall, environment='live')
        assert db.get(ConversationLabelSync, 1).status == 'pending'
    sync(session_factory, monkeypatch, fake)
    with session_factory() as db:
        row = live.session_for(db, 1)
        service.tick(db, session_id=row.id, wall_now=wall, environment='live')
        assert not any(m.get('status') == 'draft' for m in row.messages)
        assert not db.scalar(select(OutboundMessage.id))
        assert service.state(row)['next_check_at']
        event(db, 'message_created', ['ai', 'AI跟进'], 31, 103, 'incoming')
        assert row.controls['human'] and not row.controls['human_followup']['enabled']
        assert db.get(ConversationState, 1).effective_ai_state == 'HUMAN_HANDOFF'
