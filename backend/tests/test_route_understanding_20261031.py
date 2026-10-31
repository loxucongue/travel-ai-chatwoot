"""Intent comes from the semantic node, with independently verified provenance."""
from copy import deepcopy
from types import SimpleNamespace
import json
import time

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.automation_models import AutomationRun
from app.model_transport import EvaluationCallError
from app.reception_v3 import runtime, service, route_understanding as semantic
from app.reception_v3.route_choice import choice_error
from test_reception_v3_skills import context, model, final

NINE, ELEVEN = 'peach_9d_2027', 'peach_11d_2027'


def ctx(text='桃花9日'):
    c = context(ELEVEN)
    c.update(profile={'discussion_route': ELEVEN, 'party_size': 2}, state={},
             messages=[{'id': 'offer', 'direction': 'outgoing', 'status': 'read',
                        'content': '有桃花＋珠峰11日、以及不上珠峰的桃花9日，您先看哪條？'},
                       {'id': 'customer', 'direction': 'incoming', 'content': text}])
    return c


def result(c, intent='select', target=NINE):
    return {'intent': intent, 'selected_route': target if intent == 'select' else '',
            'discussed_routes': [target] if target else [],
            'evidence': [{'message_id': 'customer', 'quote': c['messages'][-1]['content']}],
            'offered_options': [{'route': ELEVEN, 'message_id': 'offer', 'quote': '桃花＋珠峰11日'},
                                {'route': NINE, 'message_id': 'offer', 'quote': '不上珠峰的桃花9日'}],
            'has_question': False, 'clarification': ''}


def semantic_model(monkeypatch, responses):
    requests = []
    def post(payload, timeout):
        requests.append(deepcopy(payload))
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return httpx.Response(200, request=httpx.Request('POST', 'https://test.invalid'), json={
            'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}],
            'usage': {'prompt_tokens': 50, 'completion_tokens': 15}})
    monkeypatch.setattr(settings, 'reception_route_understanding_enabled', True)
    monkeypatch.setattr(semantic, '_post_with_deadline', post)
    return requests


@pytest.mark.parametrize('text', ['桃花9日', '九日', '第二條', '不上珠峰那個', '剛才誤點，我是要九日'])
def test_semantic_selection_replaces_action_word_allowlist(monkeypatch, text):
    c = ctx(text)
    small = semantic_model(monkeypatch, [result(c)])
    main = model(monkeypatch, [final(route_variant=NINE, route_choice='selected', interrupt=True,
                                    start_introduction=True)] * 2)
    d, logs, _ = runtime.run_agent(c)
    assert d['route_variant'] == NINE and d['start_introduction']
    assert len(small) == 1 and small[0]['reasoning_effort'] == 'low'
    assert any(log.get('node') == 'route_understanding' for log in logs)
    assert any('route_understanding_instruction' in m.get('content', '') for m in main[0]['messages'])
    assert c['profile']['discussion_route'] == ELEVEN  # Input snapshot is immutable.


@pytest.mark.parametrize('intent', ['inquire', 'compare', 'decline', 'acknowledge', 'other', 'ambiguous'])
def test_nonselection_cannot_be_changed_by_response_generator(intent):
    c = ctx('桃花9日多少錢？')
    c['route_understanding'] = result(c, intent)
    d = runtime.Decision(route_variant=NINE, route_choice='selected', interrupt=True).model_dump()
    assert choice_error(d, c)
    d.update(route_variant=ELEVEN, route_choice='recommend', profile={'discussion_route': NINE})
    assert choice_error(d, c) is None


def test_inquiry_then_explicit_selection_are_separate_turns(monkeypatch):
    c = ctx('桃花9日多少錢？')
    requests = semantic_model(monkeypatch, [result(c, 'inquire')])
    bad = final(route_variant=NINE, route_choice='selected', interrupt=True)
    good = final(route_variant=ELEVEN, route_choice='recommend', profile={'discussion_route': NINE},
                 interrupt=True, messages=[{'text': '先看九日費用的說明。'}])
    model(monkeypatch, [bad, good, good])
    d, logs, _ = runtime.run_agent(c)
    assert d['route_variant'] == ELEVEN and not d['start_introduction']
    assert len(requests) == 1 and any(l.get('status') == 'route_choice_rejected' for l in logs)


def test_choice_repair_contains_rejected_candidate_and_authority_after_product_tools(monkeypatch):
    c = ctx('桃花9日多少錢？')
    semantic_model(monkeypatch, [result(c, 'inquire')])
    bad = final(route_variant=NINE, route_choice='selected', interrupt=True)
    good = final(route_variant=ELEVEN, route_choice='recommend', profile={'discussion_route': NINE},
                 messages=[{'text': '這裡先提供九日的費用資料。'}])
    requests = model(monkeypatch, [bad, good, good])
    decision, _, _ = runtime.run_agent(c)
    assert decision['route_choice'] == 'recommend' and decision['route_variant'] == ELEVEN
    assert any(m['role'] == 'assistant' and '"route_choice": "selected"' in (m.get('content') or '')
               for m in requests[1]['messages'])
    for request in requests:
        assert request['messages'][-1]['role'] == 'system'
        assert '"intent": "inquire"' in request['messages'][-1]['content']


def test_semantic_nonselection_allows_existing_route_introduction_after_intake():
    c = ctx('我們兩位，四月初'); c['route_understanding'] = result(c, 'other', ELEVEN)
    decision = runtime.Decision(route_variant=ELEVEN, start_introduction=True).model_dump()
    assert choice_error(decision, c) is None


def test_real_ambiguity_is_one_question_without_full_generation_or_handoff(monkeypatch):
    c = ctx('好啊')
    value = result(c, 'ambiguous'); value['clarification'] = '您想看九日，還是包含珠峰的十一日呢？'
    semantic_model(monkeypatch, [value])
    model(monkeypatch, [])
    d, logs, _ = runtime.run_agent(c)
    assert d['action'] == 'reply' and not d['route_variant'] and not d['start_introduction']
    assert d['interrupt'] and d['messages'][0]['text'] == value['clarification']
    assert semantic.from_run(SimpleNamespace(trace={'logs': logs}, input_snapshot=c)) == value


@pytest.mark.parametrize('mutation', ['advisor_as_customer', 'old_customer', 'invented_quote', 'unknown_route', 'draft_offer'])
def test_provenance_rejects_fake_or_wrong_role_evidence(mutation):
    c = ctx(); r = result(c)
    if mutation == 'advisor_as_customer': r['evidence'][0]['message_id'] = 'offer'
    elif mutation == 'old_customer':
        c['messages'].insert(0, {'id': 'old', 'direction': 'incoming', 'content': '桃花9日'})
        r['evidence'][0]['message_id'] = 'old'
    elif mutation == 'invented_quote': r['evidence'][0]['quote'] = '我要報名'
    elif mutation == 'unknown_route': r['selected_route'] = 'invented'
    else: c['messages'][0]['status'] = 'draft'
    with pytest.raises(ValueError): semantic.validate(r, semantic.task(c))


def test_context_keeps_multiple_offers_and_merged_corrections_and_omits_private():
    c = ctx()
    c['messages'] += [{'id': 'private', 'direction': 'outgoing', 'private': True, 'status': 'read', 'content': 'PRIVATE'},
                      {'id': 'unsent', 'direction': 'outgoing', 'status': 'failed', 'content': 'UNSENT'},
                      {'id': 'correction', 'direction': 'incoming', 'content': '抱歉我想看11日'}]
    c['buffered_questions'] = [c['messages'][1], c['messages'][-1]]
    data = semantic.task(c)
    assert data['current_message_ids'] == ['customer', 'correction']
    assert 'PRIVATE' not in json.dumps(data) and 'UNSENT' not in json.dumps(data)
    r = result(ctx())
    assert len(semantic.validate(r, semantic.task(ctx()))['offered_options']) == 2


def test_execution_reuses_verified_inference_and_rejects_stale_input(monkeypatch):
    c = ctx(); semantic_model(monkeypatch, [result(c)])
    value, logs, _ = semantic.interpret(c, time.monotonic() + 20)
    run = SimpleNamespace(input_snapshot=c, trace={'logs': logs})
    assert semantic.from_run(run) == value
    run.input_snapshot = deepcopy(c)
    run.input_snapshot['messages'].append({'id': 'new', 'direction': 'incoming', 'content': '不換了'})
    with pytest.raises(semantic.DecisionContractError): semantic.from_run(run)


def test_schema_repair_is_bounded_and_classified_as_nonretryable(monkeypatch):
    c = ctx(); requests = semantic_model(monkeypatch, [{}, {}])
    with pytest.raises(EvaluationCallError) as error: semantic.interpret(c, time.monotonic() + 20)
    assert len(requests) == 2 and semantic.contract_failure(error.value)


def test_semantic_schema_uses_live_catalog_and_repairs_actual_invalid_value(monkeypatch):
    c = ctx(); bad = result(c); bad['discussed_routes'] = ['invented-product']
    requests = semantic_model(monkeypatch, [bad, result(c)])
    value, logs, _ = semantic.interpret(c, time.monotonic() + 20)
    assert value['selected_route'] == NINE and logs[0]['rejected_interpretation'] == bad
    assert any(m['role'] == 'assistant' and 'invented-product' in m['content'] for m in requests[1]['messages'])
    schema = semantic.schema_for(semantic.task(c))
    assert set(schema['properties']['selected_route']['enum']) == {'', *c['skills']['routes']}


def test_network_failure_keeps_retry_classification(monkeypatch):
    semantic_model(monkeypatch, [httpx.ReadTimeout('timeout')])
    with pytest.raises(EvaluationCallError) as error: semantic.interpret(ctx(), time.monotonic() + 20)
    assert not semantic.contract_failure(error.value)


def test_attachments_do_not_become_guessed_text(monkeypatch):
    c = ctx('嗯嗯'); c['messages'][-1]['attachments'] = [{'id': 1, 'file_type': 'image'}]
    semantic_model(monkeypatch, [])
    assert semantic.interpret(c, time.monotonic() + 20) == (None, [], None)


def test_noncustomer_event_does_not_invoke_semantic_node(monkeypatch):
    c = ctx(); c['event'] = 'silence_due'
    semantic_model(monkeypatch, [])
    model(monkeypatch, [final(action='wait')])
    d, logs, _ = runtime.run_agent(c)
    assert d['action'] == 'wait' and not any(l.get('node') == 'route_understanding' for l in logs)


def test_contract_failure_ends_once_while_transport_keeps_retry(session_factory, monkeypatch):
    from test_reception_v3 import create, step
    failure = semantic.DecisionContractError('route_choice_not_validated')
    def broken(c): raise failure
    monkeypatch.setattr(service, 'run_agent', broken)
    with session_factory() as db:
        row = create(db, monkeypatch)
        step(db, row); step(db, row)
        assert service.state(row)['handoff']
        runs = list(db.scalars(select(AutomationRun).where(AutomationRun.session_id == row.id)))
        assert len(runs) == 1 and runs[0].trace['failure_kind'] == 'decision_contract'
        assert service.state(row)['attempts'] == 1


def test_offer_set_and_choice_are_persisted_from_audited_node(session_factory, monkeypatch):
    from test_linzhi_route_20261020 import setup_row
    c = ctx(); c.update(now='2026-10-31T03:03:47+00:00')
    c['skills'].update(digest='test', intake={'enabled': False})
    c['skills']['silence'] = {'enabled': False}
    c['skills']['reply'] = {'opening_interval_seconds': 2}
    value = result(c)
    logs = [{'node': 'route_understanding', 'status': 'completed', 'interpretation': value,
             'customer_message_ids': ['customer']}]
    with session_factory() as db:
        row = setup_row(db, monkeypatch, c)
        run = AutomationRun(session_id=row.id, module='reception_v3', generation=row.generation,
                            idempotency_key='semantic-choice', input_snapshot=c, trace={'logs': logs})
        db.add(run); db.flush()
        d = runtime.Decision(route_variant=NINE, route_choice='selected', interrupt=True,
                             profile={'_route_dialogue': {'intent': 'FORGED'}},
                             messages=[{'text': '可以，接著看九日行程。'}]).model_dump()
        service.apply_decision(db, row, run, c['skills'], d)
        assert row.controls['route_variant'] == NINE and row.memory['party_size'] == 2
        assert row.memory['_route_dialogue']['intent'] == 'select'
        assert len(row.memory['_route_dialogue']['offered_options']) == 2
        assert row.memory['_route_dialogue']['run_id'] == run.id
        assert not row.controls['human']


def test_same_fault_fingerprint_survives_operator_return_but_not_new_input(monkeypatch):
    c = ctx(); key = semantic.incident_key(c, 'route_choice_not_validated', 'v1')
    changed = deepcopy(c); changed['state']['attempts'] = 0
    assert semantic.incident_key(changed, 'route_choice_not_validated', 'v1') == key
    changed['messages'][-1]['id'] = 'next-customer'
    assert semantic.incident_key(changed, 'route_choice_not_validated', 'v1') != key
    assert semantic.incident_key(c, 'route_choice_not_validated', 'v2') != key
    c['skills']['digest'] = 'new-price-version'
    assert semantic.incident_key(c, 'route_choice_not_validated', 'v1') != key


def test_generator_cannot_restart_old_intro_after_customer_selected_other():
    c = ctx(); c['route_understanding'] = result(c)
    d = runtime.Decision(route_variant=ELEVEN, start_introduction=True).model_dump()
    assert choice_error(d, c)
    d.update(start_introduction=False)  # A factual restriction can be explained first.
    assert choice_error(d, c) is None


@pytest.mark.parametrize('event,unanswered', [('silence_due',False),('introduction_completed',False),
                                            ('introduction_completed',True)])
def test_optional_failed_outreach_waits_but_does_not_hide_customer_question(session_factory,monkeypatch,event,unanswered):
    from test_reception_v3 import create, step
    def broken(c): raise semantic.DecisionContractError('route_choice_not_validated')
    monkeypatch.setattr(service,'run_agent',broken)
    with session_factory() as db:
        row=create(db,monkeypatch)
        value=service.state(row)
        row.messages=[m for m in row.messages if m.get('direction')=='incoming']
        value.update(opening_started=True,delivery_kind=None,delivery_due_at=None,model_not_before=None,
                     pending_event=event,buffered_questions=([row.messages[0]['id']] if unanswered else []))
        service.save(row,value);db.commit()
        step(db,row,0)
        assert bool(service.state(row).get('handoff')) == unanswered
        run=db.scalar(select(AutomationRun))
        assert run.trace['failure_kind']=='decision_contract'
        if not unanswered:
            assert service.state(row)['followup_skip_reason']=='decision_contract_failed'
            assert not service.state(row).get('retry_at')


def test_operator_retry_keeps_incident_and_does_not_notify_again(session_factory,monkeypatch):
    from live_fixture import setup
    from test_live_audit_fixes import session as new_session
    from test_reception_v3 import bundle
    from app.models import ConversationState, MessageEvent, Notification, HandoffTask
    from app.reception_v3 import live
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(service,'compile_skills',lambda *a:deepcopy(bundle()))
    monkeypatch.setattr(service,'active_web_facts',lambda *a,**k:([],'test'))
    monkeypatch.setattr(service,'candidate_materials',lambda *a:[])
    def broken(c): raise semantic.DecisionContractError('route_choice_not_validated')
    monkeypatch.setattr(service,'run_agent',broken)
    with session_factory() as db:
        row=new_session(db)
        row.messages=[{'id':'cw-100','direction':'incoming','content':'test','created_at':row.virtual_now}]
        v=service.state(row);v.update(opening_started=True,buffered_questions=['cw-100'])
        service.save(row,v);db.commit()
        service.tick(db,session_id=row.id,environment='live',advance_clock=False)
        assert service.state(row)['handoff']
        first=list(db.scalars(select(Notification)))
        assert first
        conv=db.get(ConversationState,1)
        conv.labels=['ai']
        live.return_by_ai_label(db,conv);db.commit()
        assert service.state(row)['pending_event']=='customer_message'
        service.tick(db,session_id=row.id,environment='live',advance_clock=False)
        runs=list(db.scalars(select(AutomationRun).order_by(AutomationRun.id)))
        assert len(runs)==2 and runs[-1].trace.get('duplicate_contract_incident'), [(r.status,r.error_code,r.trace) for r in runs]
        assert len(list(db.scalars(select(Notification))))==len(first)
        assert service.state(row)['handoff']
        tasks=list(db.scalars(select(HandoffTask).order_by(HandoffTask.id)))
        assert tasks[0].status=='cancelled' and tasks[-1].status=='pending'
