"""No post-generation sentence deletion; optional touches are all-or-nothing."""
from copy import deepcopy
import json
import time
import httpx
import pytest
from app.config import settings
from app.reception_v3 import repetition
from app.reception_v3.runtime import Decision


PRICE = '2027桃花＋珠峰11日的出發區間是3/20–4/10，團費標準是人民幣12,780元/人（兩人一房）。\n'
INCLUDED = '包含：高原保障險、入藏文件、住宿。\n不含：機票、午晚餐、小費。\n'
QUESTION = '方便問一下這次大概幾位一起出發呢？'


def draft(**extra):
    return Decision(messages=[{'text':PRICE+INCLUDED+QUESTION}], **extra).model_dump()


def context(event='customer_message', human=False):
    return {'event':event, 'state':{'human_followup':human}, 'messages':[
        {'direction':'outgoing','content':'出發區間是3月20日到4月10日。請問幾位？','status':'sent'},
        {'direction':'outgoing','content':'UNSENT','status':'draft'},
        {'direction':'outgoing','content':'PRIVATE','status':'sent','private':True},
        {'direction':'incoming','content':'11日價位'}]}


@pytest.mark.parametrize('event', ['customer_message', 'introduction_completed', 'intake_due', None])
@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('enabled', [False, True])
def test_active_and_buffered_answers_never_call_reviewer(monkeypatch, event, human, enabled):
    # Replays the mixed date/first-price sentence from production run 3971.
    # Protect the entire answer, not merely strings matching a price regex.
    monkeypatch.setattr(settings, 'reception_repeat_review_enabled', enabled)
    monkeypatch.setattr(repetition, '_post_with_deadline', lambda *a: pytest.fail('active answer must not be reviewed'))
    decision = draft(profile={'party_size':4})
    original = deepcopy(decision)
    result, logs, digest = repetition.review(decision, context(event, human), time.monotonic()-1)
    assert result is decision and result == original
    assert not logs and digest is None


def response(ids):
    return httpx.Response(200, request=httpx.Request('POST','https://test.invalid'), json={
        'choices':[{'message':{'content':json.dumps({'remove_clause_ids':ids})}}],
        'usage':{'prompt_tokens':100,'completion_tokens':10}})


@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('ids', [[], [0], [1,2]])
def test_proactive_review_preserves_whole_answer_or_sends_nothing(monkeypatch, human, ids):
    monkeypatch.setattr(settings, 'reception_repeat_review_enabled', True)
    requests = []
    def post(payload, timeout):
        requests.append(payload)
        return response(ids)
    monkeypatch.setattr(repetition, '_post_with_deadline', post)
    decision = draft()
    # No orphan image, template or follow-up question when a caption is rejected.
    decision['messages'] += [{'asset_keys':['photo'], 'text':'圖文'}, {'script_id':'approved'}]
    original = deepcopy(decision)
    result, logs, digest = repetition.review(decision, context('silence_due',human), time.monotonic()+20)
    assert decision == original and digest
    if ids:
        assert result['action']=='wait' and result['messages']==[]
    else:
        assert result is decision
    assert logs[0]['followup_suppressed'] == bool(ids)
    assert 'removed_clauses' not in logs[0]
    assert 'UNSENT' not in str(requests) and 'PRIVATE' not in str(requests)
    assert '整条跟进不发' in requests[0]['messages'][0]['content']


@pytest.mark.parametrize('ids', [[-1], [99], [True], ['1'], [0,0], None, [[0]]])
def test_invalid_review_never_sends_unreviewed_followup(monkeypatch, ids):
    monkeypatch.setattr(settings, 'reception_repeat_review_enabled', True)
    monkeypatch.setattr(repetition, '_post_with_deadline', lambda *a: response(ids))
    result, logs, _ = repetition.review(draft(), context('silence_due'), time.monotonic()+20)
    assert result['action']=='wait' and result['messages']==[]
    assert logs[0]['status']=='failed'


@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('expired', [False, True])
def test_unavailable_review_defers_optional_touch(monkeypatch, human, expired):
    monkeypatch.setattr(settings, 'reception_repeat_review_enabled', True)
    def fail(*a):
        raise TimeoutError('test_timeout')
    monkeypatch.setattr(repetition, '_post_with_deadline', fail)
    result, logs, _ = repetition.review(draft(), context('silence_due',human),
                                        time.monotonic()+(-1 if expired else 20))
    assert result['action']=='wait' and not result['messages']
    assert logs[0]['status']==('skipped' if expired else 'failed')


def test_suppressed_touch_does_not_persist_uncommunicated_route_offer(monkeypatch):
    monkeypatch.setattr(settings, 'reception_repeat_review_enabled', True)
    monkeypatch.setattr(repetition, '_post_with_deadline', lambda *a: response([0]))
    decision = draft(route_variant='peach_11d_2027', route_choice='recommend', profile={'discussion_route':'peach_11d_2027'})
    result, _, _ = repetition.review(decision, context('silence_due'), time.monotonic()+20)
    assert not result['route_variant'] and not result['profile'] and result['route_choice']=='unchanged'


def test_bypass_is_not_authorization_for_unvalidated_price():
    from app.reception_v3.seasonal_quotes import quote_error
    decision, _, _ = repetition.review(draft(), context(), time.monotonic()+20)
    decision['route_variant']='tibet_essence_8d'
    assert quote_error(decision, {'routes':{'tibet_essence_8d':{'seasonal_pricing':True,'scripts':[]}}}, set())


def test_suppressed_timer_does_not_restart_intake_or_emit_any_message(session_factory, monkeypatch):
    from app.automation_models import AutomationRun
    from app.reception_v3 import service
    from test_reception_v3 import create, bundle
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.controls = {**row.controls, 'route_variant':'nine'}
        value = service.state(row)
        value.update(opening_started=True, delivery_kind=None, completed_introductions=[])
        service.save(row, value)
        before = deepcopy(row.messages)
        monkeypatch.setattr(service, 'begin_intake', lambda *a: pytest.fail('timer wait must not start intake'))
        run = AutomationRun(session_id=row.id, generation=row.generation, module='silence_touch',
                            idempotency_key='suppressed-timer', input_snapshot={'event':'silence_due'})
        db.add(run); db.flush()
        service.apply_decision(db, row, run, bundle(), repetition.wait_for_next_touch(draft()))
        assert row.messages == before
        assert service.state(row)['next_check_at']
