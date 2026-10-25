"""October 24 first batch: isolated DB, mocked Chatwoot/model, no live sends."""
from copy import deepcopy
import json
import pytest
from sqlalchemy import select

from app import conversation_labels, engagement_labels
from app.models import ConversationState, ConversationLabelSync, MessageEvent, OutboundMessage
from app.automation_models import AutomationRun
from app.reception_v3 import live, service, human_followup as hf, runtime
from app.reception_v3.seasonal_quotes import month_answer_window
from app.reception_config import default_reception_configuration
from scripts.configure_small_fixes_20261024 import updated
from test_business_feedback_20261019 import seed, queue
from test_human_return_20261016 import prepared, arm, activate, sync
from test_live_audit_fixes import event
from test_seasonal_runtime_journeys import runtime_context
from test_reception_v3_skills import model, final


def test_candidate_changes_only_party_intake_wait():
    before = default_reception_configuration()
    before['intake']['wait_seconds'] = 120
    before['silence'].update(daily_at='09:00', max_proactive_messages_per_day=1)
    snapshot = deepcopy(before)
    after = updated(before)
    assert before == snapshot and after['intake']['wait_seconds'] == 90
    assert updated(after) == after
    after['intake']['wait_seconds'] = 120
    assert after == before


@pytest.mark.parametrize('title', [
    '西藏精華-8天 拉薩+珠峰', '西藏精華－8天 拉薩＋珠峰',
    '西藏精華8天・拉薩＋珠峰', '西藏精華8天（拉薩+珠峰）',
    '3~4月｜西藏精華-8天 拉薩+珠峰',
])
def test_advertisement_separator_variants_do_not_tag_engagement(session_factory, monkeypatch, title):
    seed(session_factory, monkeypatch)
    from test_reception_v3 import bundle
    spec = bundle()
    spec['routes']['eight'] = {'name': '西藏精華8天・拉薩＋珠峰',
                               'aliases': ['西藏精華8天-拉薩+珠峰']}
    monkeypatch.setattr('app.reception_v3.skills.compile_skills', lambda db: deepcopy(spec))
    assert queue(session_factory, title) is None
    assert queue(session_factory, title + ' 多少錢？') is not None


@pytest.mark.parametrize('kind', ['outgoing', 'activity', 'private'])
def test_engagement_observer_itself_excludes_non_customer_messages(session_factory, monkeypatch, kind):
    seed(session_factory, monkeypatch)
    with session_factory() as db:
        message = MessageEvent(conversation_state_id=1, chatwoot_message_id=1001,
            direction='incoming' if kind=='private' else kind, private=kind=='private', content='hello')
        db.add(message); db.flush()
        engagement_labels.observe_reply(db, db.get(ConversationState, 1), message)
        assert not engagement_labels.pending_labels(db, 1)


@pytest.mark.parametrize('label', ['加私訊', '加私讯'])
def test_private_contact_label_cancels_thirty_minute_wait_and_cannot_be_overridden(session_factory, monkeypatch, label):
    task = prepared(session_factory, monkeypatch)
    arm(session_factory, task); sync(session_factory, monkeypatch)
    with session_factory() as db:
        row = live.session_for(db, 1)
        due = row.controls['human_followup']['resume_at']
        event(db, 'conversation_updated', ['人工接管', label], 30)
        assert not hf.enabled(row)
        assert row.controls['simulation']['status'] == 'stopped'
        assert not service.state(row).get('next_check_at')
        row.virtual_now = due
        hf.activate_due(db, db.get(ConversationState, 1), row)
        event(db, 'conversation_updated', ['ai', label], 31)
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')
        assert not live.permitted(db, db.get(ConversationState, 1))
        assert row.controls['simulation']['status'] == 'stopped'
        assert not db.scalar(select(OutboundMessage.id))


@pytest.mark.parametrize('source', ['conversation', 'contact'])
def test_private_contact_before_advisor_reply_cannot_arm(session_factory, monkeypatch, source):
    task = prepared(session_factory, monkeypatch)
    with session_factory() as db:
        conv = db.get(ConversationState, 1)
        target = conv if source=='conversation' else conv.contact
        target.labels = [*(target.labels or []), '加私訊']
        db.commit()
    with pytest.raises(ValueError, match='保护标签'):
        arm(session_factory, task)


@pytest.mark.parametrize('blocker', ['remote_private', 'contact_private', 'inbox_off'])
def test_new_blocker_wins_over_already_queued_ai_label_write(session_factory, monkeypatch, blocker):
    task = prepared(session_factory, monkeypatch)
    arm(session_factory, task); fake = sync(session_factory, monkeypatch)
    activate(session_factory)
    if blocker == 'remote_private':
        # Deliberately no local webhook: it has not arrived yet.
        fake.current_labels.append('加私訊')
    else:
        with session_factory() as db:
            conv = db.get(ConversationState, 1)
            if blocker == 'contact_private': conv.contact.labels = ['加私訊']
            else: conv.inbox.ai_enabled = False
            db.commit()
    fake.calls.clear()
    sync(session_factory, monkeypatch, fake)
    assert not any(call[0]=='labels' for call in fake.calls)
    assert 'ai' not in fake.current_labels and '人工接管' in fake.current_labels
    with session_factory() as db:
        assert db.get(ConversationLabelSync, 1).status == 'cancelled'
        row = live.session_for(db, 1)
        assert not hf.enabled(row) and row.controls['simulation']['status']=='stopped'
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')


@pytest.mark.parametrize('blocker', ['private', 'inbox'])
def test_blocked_channel_or_label_cancels_unsent_draft(session_factory, monkeypatch, blocker):
    seed(session_factory, monkeypatch)
    with session_factory() as db:
        conv = db.get(ConversationState, 1)
        if blocker == 'private': conv.labels = ['ai', '加私訊']
        else: conv.inbox.ai_enabled = False
        row = live.session_for(db, 1)
        row.messages = [{'id':'pending', 'content':'不應送出', 'direction':'outgoing', 'status':'draft'}]
        service.save(row, {**service.state(row), 'delivery_due_at':row.virtual_now})
        monkeypatch.setattr(live, 'client_for', lambda _: pytest.fail('blocked draft must not call Chatwoot'))
        live.deliver_one(db, row)
        assert row.controls['simulation']['status']=='stopped'
        assert not any(m.get('status')=='draft' for m in row.messages)
        assert not db.scalar(select(OutboundMessage.id))


def test_disabled_inbox_prevents_manual_ai_takeover_and_due_activation(session_factory, monkeypatch):
    task = prepared(session_factory, monkeypatch)
    arm(session_factory, task); sync(session_factory, monkeypatch)
    with session_factory() as db:
        conv = db.get(ConversationState, 1)
        conv.inbox.ai_enabled = False
        row = live.session_for(db, 1)
        row.virtual_now = row.controls['human_followup']['resume_at']
        hf.activate_due(db, conv, row)
        assert not hf.enabled(row)
        event(db, 'conversation_updated', ['ai'], 40)
        assert not live.permitted(db, conv) and not live.permitted(db, conv, purpose='followup')
        assert row.controls['simulation']['status']=='stopped'


@pytest.mark.parametrize('blocker', ['private', 'inbox'])
def test_blocked_reception_never_calls_model(session_factory, monkeypatch, blocker):
    seed(session_factory, monkeypatch)
    with session_factory() as db:
        conv = db.get(ConversationState, 1)
        if blocker=='private': conv.labels = ['ai', '加私訊']
        else: conv.inbox.ai_enabled = False
        db.commit()
        row = live.session_for(db, 1)
        monkeypatch.setattr(service, 'run_agent', lambda _: pytest.fail('blocked reception called model'))
        service.tick(db, session_id=row.id, environment='live', advance_clock=False)
        assert not db.scalar(select(AutomationRun.id))


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('blocker', ['inbox', 'tenant', 'private', 'contact_private'])
def test_disabled_during_model_discards_answer_and_profile(session_factory, monkeypatch, failed, blocker):
    seed(session_factory, monkeypatch)
    from test_reception_v3 import bundle, answer
    monkeypatch.setattr(service, 'compile_skills', lambda db: bundle())
    monkeypatch.setattr(service, 'active_web_facts', lambda *a, **k: ([], 'test'))
    monkeypatch.setattr(service, 'candidate_materials', lambda *a: [])
    def respond(_):
        with session_factory() as other:
            from app.models import Tenant
            conv = other.get(ConversationState, 1)
            if blocker == 'inbox': conv.inbox.ai_enabled = False
            elif blocker == 'tenant': other.get(Tenant, conv.tenant_id).ai_enabled = False
            elif blocker == 'private': conv.labels = ['ai', '加私訊']
            else: conv.contact.labels = ['加私訊']
            other.commit()
        if failed:
            raise RuntimeError('simulated model failure after inbox disabled')
        return answer(profile={'party_size':99}, messages=[{'text':'不應送出'}])
    monkeypatch.setattr(service, 'run_agent', respond)
    with session_factory() as db:
        row=live.session_for(db, 1)
        service.tick(db, session_id=row.id, environment='live', advance_clock=False)
        run=db.scalar(select(AutomationRun))
        assert run.status=='cancelled' and run.error_code=='reception_blocked'
        assert 'party_size' not in row.memory
        assert not any(m.get('status')=='draft' for m in row.messages)


@pytest.mark.parametrize('text,expected', [
    ('希望二月份', '2027-02-01'), ('預計3月', '2027-03-01'), ('想12月', '2026-12-01'),
    ('明年2月', '2027-02-01'), ('今年2月', '2026-02-01'), ('2028年2月', '2028-02-01'),
])
def test_month_dates_use_future_default_without_overriding_explicit_year(text, expected):
    assert month_answer_window(text, '2026-10-24T01:00:00+00:00')['start']==expected


def test_known_year_is_kept_but_current_explicit_year_wins():
    now = '2026-10-24T01:00:00+00:00'
    checked = month_answer_window('希望二月份', now, '2028年出發')
    assert checked['start']=='2028-02-01' and checked['end']=='2028-02-29'
    assert checked['year_inferred'] is False
    assert checked['customer_month']=='2028年2月'
    assert month_answer_window('明年二月', now, '2028年出發')['start']=='2027-02-01'
    assert month_answer_window('二月', now, '2027年或2028年') is None


@pytest.mark.parametrize('known', ['明年出發', '今年出發', '後年再去'])
def test_relative_year_history_is_not_overwritten_by_automatic_month_default(known):
    now = '2026-10-24T01:00:00Z'
    assert month_answer_window('12月', now, known) is None
    assert month_answer_window('2028年12月', now, known)['start'] == '2028-12-01'


@pytest.mark.parametrize('text', ['希望十月份', '今年十月', '2026年10月'])
def test_current_month_checks_remaining_dates_without_false_past_year_warning(text):
    window = month_answer_window(text, '2026-10-24T01:00:00+00:00')
    assert window['start'] == '2026-10-24' and window['end'] == '2026-10-31'


def test_month_resolution_uses_local_calendar_and_keeps_explicit_past_year():
    assert month_answer_window('一月', '2026-12-31T16:00:00Z')['start'] == '2027-01-01'
    assert month_answer_window('今年二月', '2026-10-24T01:00:00Z')['start'] == '2026-02-01'


def test_current_month_runtime_preserves_notice_check_instead_of_asking_year(monkeypatch):
    c = runtime_context()
    c['now'] = '2026-10-24T01:00:00+00:00'
    c['messages'] = [{'id':'month', 'direction':'incoming', 'content':'希望十月份'}]
    requests = model(monkeypatch, [final(action='wait')])
    runtime.run_agent(c)
    checked = json.loads(requests[0]['messages'][-1]['content'])['departure_review']
    assert checked['result']['status'] == 'advisor'
    assert '不足30天' in checked['result']['reason']


@pytest.mark.parametrize('text', ['不想二月', '希望二月份不要去', '二月份嗎？', '二月或三月', '3-4月', '三月中'])
def test_uncertain_dates_are_not_silently_reinterpreted(text):
    assert month_answer_window(text, '2026-10-24T01:00:00+00:00') is None


def test_month_short_answer_reaches_runtime_with_year_evidence(monkeypatch):
    c = runtime_context()
    c['now'] = '2026-10-24T01:00:00+00:00'
    c['messages'] = [{'id':'month', 'direction':'incoming', 'content':'希望二月份'}]
    requests = model(monkeypatch, [final(action='wait')])
    runtime.run_agent(c)
    current = json.loads(requests[0]['messages'][-1]['content'])
    assert current['departure_review']['start']=='2027-02-01'
    assert current['departure_review']['end']=='2027-02-28'
    assert current['departure_review']['year_inferred'] is True
    assert '首次推荐具体团期' in requests[0]['messages'][0]['content']
