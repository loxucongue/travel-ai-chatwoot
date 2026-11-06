"""Regression for #644: the model's material menu must be executable."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.automation_models import AutomationRun
from app.reception_v3 import service
from app.reception_v3.material_rules import eligible_rules
from app.reception_v3.route_understanding import DecisionContractError
from app.reception_v3.runtime import Decision
from test_reception_v3 import create, bundle, step


def catalog():
    return [dict(key='shared', routes=['nine', 'eleven'], content_type='image', media_hash='shared'),
            dict(key='old-only', routes=['nine'], content_type='image', media_hash='old'),
            dict(key='new-only', routes=['eleven'], content_type='image', media_hash='new')]


def rules():
    return [dict(id=m['key'], enabled=True, route_variants=m['routes'], purpose='followup',
                 any_labels=[], asset_keys=[m['key']], approved_text=m['key']) for m in catalog()]


def prepared(db, monkeypatch):
    row = create(db, monkeypatch)
    row.controls = {**row.controls, 'route_variant': 'nine'}
    row.memory = {'discussion_route': 'eleven'}
    row.messages = [m for m in row.messages if m['direction'] == 'incoming']
    spec = bundle()
    spec['routes']['eleven'] = deepcopy(spec['routes']['nine'])
    spec['material_rules'] = rules()
    spec['silence']['material_selection'] = 'configured_rules'
    monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(spec))
    monkeypatch.setattr(service, 'candidate_materials', lambda *a: catalog())
    monkeypatch.setattr(service, 'resolve_materials', lambda db, keys, *a: [
        dict(content_type='image', asset_key=k, media_hash=k) for k in keys])
    return row, spec


def test_comparison_allows_only_shared_materials_and_keeps_route(session_factory, monkeypatch):
    with session_factory() as db:
        row, spec = prepared(db, monkeypatch)
        choices = eligible_rules(spec, service.followup_materials(catalog(), row.messages, 'nine', row.memory),
                                 'nine', [], 'silence_due', row.messages, row.memory)
        assert [r['id'] for r in choices] == ['shared']
        decision = Decision(route_variant='nine', messages=[{'material_rule_id': 'shared'}]).model_dump()
        route, parts = service.parts_for(db, row, spec, decision, followup=True)
        assert route == 'nine' and row.memory == {'discussion_route': 'eleven'}
        assert [p['asset_key'] for p in parts if p['content_type'] == 'image'] == ['shared']
        row.messages += [{**p, 'direction': 'outgoing', 'status': 'read'} for p in parts]
        _, again = service.parts_for(db, row, spec, decision, followup=True)
        assert not any(p['content_type'] == 'image' for p in again)


@pytest.mark.parametrize('rule_id', ['old-only', 'new-only', 'unknown'])
def test_specific_or_unknown_rule_is_not_sent_as_shared(session_factory, monkeypatch, rule_id):
    with session_factory() as db:
        row, spec = prepared(db, monkeypatch)
        d = Decision(route_variant='nine', messages=[{'material_rule_id': rule_id}]).model_dump()
        with pytest.raises(DecisionContractError, match='material_rule_not_eligible'):
            service.parts_for(db, row, spec, d, followup=True)
        assert not any(m.get('status') == 'draft' for m in row.messages)


def test_model_menu_and_executor_share_material_scope(session_factory, monkeypatch):
    seen = []
    def model(context):
        seen.append(context)
        assert [r['id'] for r in context['material_rules']] == ['shared']
        return Decision(route_variant='nine', messages=[{'material_rule_id':'shared'}]).model_dump(), [], 'test'
    with session_factory() as db:
        row, _ = prepared(db, monkeypatch)
        monkeypatch.setattr(service, 'run_agent', model)
        service.save(row, dict(opening_started=True, pending_event='silence_due', buffered_questions=[],
                              followup_last_customer_at=row.virtual_now))
        db.commit(); step(db, row, 0)
        run = db.scalar(select(AutomationRun).where(AutomationRun.session_id == row.id))
        assert seen and run.status == 'completed'
        assert any(m.get('asset_key') == 'shared' and m.get('status') == 'draft' for m in row.messages)
        assert not service.state(row).get('handoff')


@pytest.mark.parametrize('event,buffered,should_defer', [
    ('silence_due', [], True), ('customer_message', [], False), ('silence_due', ['question'], False)])
def test_unusable_optional_material_defers_but_unanswered_question_retries(
        session_factory, monkeypatch, event, buffered, should_defer):
    with session_factory() as db:
        row, _ = prepared(db, monkeypatch)
        row.messages += [dict(id='question', direction='incoming', content='住宿如何？', created_at=row.virtual_now)]
        service.save(row, dict(opening_started=True, pending_event=event, buffered_questions=buffered,
                              followup_last_customer_at=row.virtual_now))
        monkeypatch.setattr(service, 'run_agent', lambda c: (
            Decision(route_variant='nine', messages=[{'material_rule_id':'unknown'}]).model_dump(), [], 'test'))
        db.commit(); step(db, row, 0)
        state = service.state(row)
        run = db.scalar(select(AutomationRun).where(AutomationRun.session_id == row.id))
        assert run.status == 'failed' and run.trace['failure_kind'] == 'decision_contract'
        assert not state.get('handoff') and not row.controls.get('human')
        if should_defer:
            assert state['next_check_at'] and not state.get('retry_at') and not state.get('attempts')
            assert state['followup_skip_reason'] == 'decision_contract_failed'
        else:
            assert state['retry_at'] and state['attempts'] == 1


def test_delivery_scope_honors_operator_disable_labels_and_changed_discussion():
    spec = {'material_rules': rules()}
    args = (spec, [], 'nine', [], 'silence_due', [])
    assert [r['id'] for r in eligible_rules(*args, {'discussion_route':'eleven'})] == ['shared']
    spec['material_rules'][0]['enabled'] = False
    assert eligible_rules(*args, {'discussion_route':'eleven'}) == []
    spec['material_rules'][0].update(enabled=True, any_labels=['allowed'])
    assert eligible_rules(*args, {'discussion_route':'eleven'}) == []
    assert eligible_rules(spec, [], 'nine', ['allowed'], 'silence_due', [], {'discussion_route':'another'}) == []


def test_rejected_route_stays_excluded_even_with_shared_assets():
    media = [dict(key='star', routes=['tibet_overview_10d', 'peach_9d_2027'], content_type='image')]
    profile = {'discussion_route':'peach_9d_2027', 'everest_preference':'avoid'}
    assert service.followup_materials(media, [], 'tibet_overview_10d', profile) == []
