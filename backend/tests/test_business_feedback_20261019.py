from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import pytest
from sqlalchemy import select
from app import engagement_labels as engaged, conversation_labels, live_reply
from app.models import (ConversationState, ConversationLabelAddition, ConversationLabelSync,
                        MessageEvent, WebhookEvent, OutboundMessage, utcnow)
from app.reception_v3 import live, service
from app.runtime_settings import dt
from live_fixture import setup
from test_live_audit_fixes import session, event
from test_conversation_controls import FakeChatwoot
from test_human_return_20261016 import prepared, arm, activate, sync
from test_reception_v3 import bundle
from test_winter_spring_20261017 import winter_catalog
from scripts.configure_tsozong_20261019 import update_package, ROUTES, ANSWER, GUIDANCE


def seed(factory, monkeypatch):
    setup(factory, monkeypatch)
    spec = bundle()
    spec['intake'] = {'options': ['自己一位', '2～3位', '4～6位', '7～10位', '10位以上']}
    monkeypatch.setattr('app.reception_v3.skills.compile_skills', lambda db: deepcopy(spec))
    with factory() as db:
        session(db)


def queue(factory, content='措宗寺有嗎？', *, complete=False):
    with factory() as db:
        row = live.session_for(db, 1)
        if complete:
            service.save(row, {'completed_introductions': ['peach_11d_2027'], 'intake_status': 'done'})
        latest = db.scalar(select(MessageEvent.chatwoot_message_id).where(MessageEvent.conversation_state_id == 1).order_by(MessageEvent.chatwoot_message_id.desc()).limit(1))
        message = MessageEvent(conversation_state_id=1, chatwoot_message_id=max(103, (latest or 0) + 1),
            direction='incoming', content=content)
        db.add(message); db.flush()
        engaged.observe_reply(db, db.get(ConversationState, 1), message)
        db.commit()
        return db.get(ConversationLabelAddition, (1, engaged.ENGAGED_LABEL))


@pytest.mark.parametrize('content', ['措宗寺有嗎？', '好', '4月', '2', '👍', '我們四位想自己包團'])
def test_any_actual_reply_is_an_engagement(session_factory, monkeypatch, content):
    seed(session_factory, monkeypatch)
    assert queue(session_factory, content).status == 'pending'


@pytest.mark.parametrize('content', ['自己一位', '2～3位', '4～6位', '7～10位', '10位以上', '我人數有4位以上，想自己包團'])
def test_initial_people_options_are_excluded(session_factory, monkeypatch, content):
    seed(session_factory, monkeypatch)
    assert queue(session_factory, content) is None


def test_product_selection_excluded_but_same_text_later_counts(session_factory, monkeypatch):
    seed(session_factory, monkeypatch)
    title = next(iter(bundle()['routes'].values()))['name']
    assert queue(session_factory, title) is None
    assert queue(session_factory, title, complete=True)


def test_ad_month_prefix_is_still_an_initial_route_option(session_factory, monkeypatch):
    seed(session_factory, monkeypatch)
    title = next(iter(bundle()['routes'].values()))['name']
    assert queue(session_factory, '3~4月｜' + title) is None
    assert queue(session_factory, '想問' + title + '的費用')


def test_late_intake_option_not_counted_after_timeout_introduction(session_factory, monkeypatch):
    seed(session_factory, monkeypatch)
    with session_factory() as db:
        db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=102, direction='outgoing',
            content_type='input_select', content='幾月出發？',
            content_attributes={'items': [{'title': '3-4月', 'value': '3-4月'}]}))
        db.commit()
    assert queue(session_factory, '3-4月', complete=True) is None
    assert queue(session_factory, '四月想看桃花', complete=True)


@pytest.mark.parametrize('kind', ['human', 'paused', 'global_off', 'empty'])
def test_no_tag_outside_ai_reception(session_factory, monkeypatch, kind):
    seed(session_factory, monkeypatch)
    with session_factory() as db:
        row = live.session_for(db, 1)
        if kind == 'human': live.handoff(db, row, 'custom', '人工接待')
        if kind == 'paused': service.control(row, 'stop')
        if kind == 'global_off':
            from app.operations import save_setting
            save_setting(db, 'global_message_sending', {'enabled': False})
        db.commit()
    assert queue(session_factory, '' if kind == 'empty' else '你好') is None


def test_sync_retry_does_not_pause_ai_and_preserves_labels(session_factory, monkeypatch):
    seed(session_factory, monkeypatch); queue(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr(engaged, 'client_for', lambda _: fake)
    original = fake.set_conversation_labels
    def fail(*args): raise RuntimeError('offline')
    monkeypatch.setattr(fake, 'set_conversation_labels', fail)
    assert engaged.process_label_addition(session_factory)
    with session_factory() as db:
        job = db.get(ConversationLabelAddition, (1, engaged.ENGAGED_LABEL))
        assert job.status == 'pending' and job.attempts == 1
        assert live.permitted(db, db.get(ConversationState, 1))
        job.due_at = utcnow(); db.commit()
    monkeypatch.setattr(fake, 'set_conversation_labels', original)
    assert engaged.process_label_addition(session_factory)
    assert set(fake.current_labels) == {'ai', '外部新增', '以開口'}
    assert not engaged.process_label_addition(session_factory)
    with session_factory() as db:
        assert live.permitted(db, db.get(ConversationState, 1))
        assert not db.scalar(select(OutboundMessage.id))


@pytest.mark.parametrize('phase', ['before_http', 'during_http'])
def test_handoff_race_keeps_new_human_owner(session_factory, monkeypatch, phase):
    seed(session_factory, monkeypatch); queue(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr(engaged, 'client_for', lambda _: fake)
    monkeypatch.setattr(conversation_labels, 'client_for', lambda _: fake)
    def handoff():
        with session_factory() as db:
            live.handoff(db, live.session_for(db, 1), 'custom', '人工核对'); db.commit()
    if phase == 'before_http': handoff()
    else:
        original = fake.set_conversation_labels
        def during(*args):
            handoff()
            return original(*args)
        monkeypatch.setattr(fake, 'set_conversation_labels', during)
    engaged.process_label_addition(session_factory)
    if phase == 'during_http': monkeypatch.setattr(fake, 'set_conversation_labels', original)
    assert conversation_labels.process_label_sync(session_factory)
    assert set(fake.current_labels) == {'人工接管', '外部新增', '以開口'}
    with session_factory() as db:
        db.get(ConversationLabelAddition, (1, engaged.ENGAGED_LABEL)).due_at = utcnow(); db.commit()
    engaged.process_label_addition(session_factory)
    with session_factory() as db:
        assert not live.permitted(db, db.get(ConversationState, 1))
        assert db.get(ConversationLabelAddition, (1, engaged.ENGAGED_LABEL)).status == 'synced'


@pytest.mark.parametrize('active', [False, True])
def test_followup_customer_reply_tagged_only_after_ai_resume(session_factory, monkeypatch, active):
    task = prepared(session_factory, monkeypatch); arm(session_factory, task)
    fake = sync(session_factory, monkeypatch)
    if active:
        activate(session_factory); sync(session_factory, monkeypatch, fake)
    with session_factory() as db:
        event(db, 'message_created', fake.current_labels, 50, 105, 'incoming')
        job = db.get(ConversationLabelAddition, (1, engaged.ENGAGED_LABEL))
        assert bool(job) == active
        assert db.get(ConversationLabelSync, 1).target == 'human'
        assert not live.permitted(db, db.get(ConversationState, 1))


@pytest.mark.parametrize('excluded', ['private', 'external_echo', 'old', 'update', 'outgoing'])
def test_webhook_excludes_non_customer_new_events(session_factory, monkeypatch, excluded):
    seed(session_factory, monkeypatch)
    with session_factory() as db:
        payload = {'id': 105, 'message_type': 'outgoing' if excluded == 'outgoing' else 'incoming',
            'content': '你好', 'created_at': (dt(utcnow()) - timedelta(minutes=10)).isoformat() if excluded == 'old' else utcnow(),
            'private': excluded == 'private', 'content_attributes': {'external_echo': excluded == 'external_echo'},
            'sender': {'type': 'agent_bot'}, 'conversation': {'id': 26, 'inbox_id': 128859, 'labels': ['ai'], 'can_reply': True, 'meta': {'sender': {'id': 55}}}}
        event_row = WebhookEvent(connection_id=1, event='message_updated' if excluded == 'update' else 'message_created',
            account_id=180474, resource_id='105', idempotency_key=excluded, payload=payload, received_at=utcnow())
        db.add(event_row); db.flush(); live_reply.mirror_event(db, event_row)
        assert not db.scalar(select(ConversationLabelAddition))


def test_duplicate_event_does_not_reclassify_initial_option(session_factory, monkeypatch):
    seed(session_factory, monkeypatch)
    with session_factory() as db:
        db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=105, direction='incoming', content='hello'))
        db.commit()
        event(db, 'message_created', ['ai'], 10, 105, 'incoming')
        assert not db.scalar(select(ConversationLabelAddition))


@pytest.mark.parametrize('route', sorted(ROUTES | {'tibet_essence_8d'}))
def test_temple_fact_is_route_scoped_idempotent_and_preserves_pricing(route):
    from app.route_packages import _validate
    original = winter_catalog()['routes'][route]
    updated = update_package(original)
    assert update_package(updated) == updated
    assert updated.get('seasonal_pricing') == original.get('seasonal_pricing')
    assert updated['content_groups'] == original['content_groups']
    _validate(deepcopy(updated), Path('route-package.json'))
    if route in ROUTES:
        assert next(s for s in updated['fixed_answers'] if s['id'] == 'basum_tsozong_temple')['answer_text'] == ANSWER
        assert GUIDANCE in updated['ai_guidance']
    else:
        assert updated == original


def test_existing_lowercase_chatwoot_followup_label_is_reused(session_factory, monkeypatch):
    task = prepared(session_factory, monkeypatch); arm(session_factory, task)
    fake = sync(session_factory, monkeypatch); activate(session_factory)
    catalog = fake.list_labels()
    catalog['payload'].append({'id': 99, 'title': 'ai跟进'})
    monkeypatch.setattr(fake, 'list_labels', lambda: catalog)
    def duplicate(*args): raise AssertionError('Chatwoot rejects a duplicate normalized title with 422')
    monkeypatch.setattr(fake, 'create_label', duplicate)
    sync(session_factory, monkeypatch, fake)
    assert set(fake.current_labels) == {'ai', 'ai跟进', '外部新增'}
    with session_factory() as db:
        conv = db.get(ConversationState, 1)
        job = db.get(ConversationLabelSync, 1)
        assert job.status == 'synced' and live.permitted(db, conv, purpose='followup')
        revision = job.revision
        conversation_labels.queue_labels(db, conv, 'followup'); db.commit()
        assert job.status == 'synced' and job.revision == revision
        live.handoff(db, live.session_for(db, 1), 'custom', '顾问接待'); db.commit()
    sync(session_factory, monkeypatch, fake)
    assert set(fake.current_labels) == {'人工接管', '外部新增'}


def test_new_chatwoot_label_normalization_is_accepted(session_factory, monkeypatch):
    task = prepared(session_factory, monkeypatch); arm(session_factory, task)
    fake = sync(session_factory, monkeypatch); activate(session_factory)
    monkeypatch.setattr(fake, 'create_label', lambda title, *args: {'id': 99, 'title': title.casefold()})
    sync(session_factory, monkeypatch, fake)
    assert set(fake.current_labels) == {'ai', 'ai跟进', '外部新增'}
    with session_factory() as db:
        assert db.get(ConversationLabelSync, 1).status == 'synced'
