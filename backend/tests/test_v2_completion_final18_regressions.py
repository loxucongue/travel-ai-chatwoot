"""Request semantics stay separate from actual delivery evidence."""
from copy import deepcopy
import pytest
from app.reception_v2.reply_scope import parse_scope, verify_scope


def audit(question):
    return dict(current_request='資料', required_answer='實際交付',
        question_checks=[question], event_error_checks=[], material_request_checks=[],
        profile_update_checks=[], traveler_age_checks=[], contact_refusals=[],
        unwanted_part_checks=[], requested_material_kinds=[], consultant_task_checks=[])


def test_reference_metadata_normalization_preserves_diagnostics():
    value=audit(dict(request_quote='介紹', request_kind='material', covered=True,
                    answer_kind='mixed', answer_segment_ids='reply_0', answer_asset_ids=None))
    value['missing_answers']={'detail':'實際仍缺小費回答'}
    result=parse_scope(value)
    assert result['question_checks'][0]['answer_segment_ids']==['reply_0']
    assert '實際仍缺小費回答' in result['missing_answers'][0]


@pytest.mark.parametrize('request_kind,asset,receipt,missing',[
    ('material','itinerary',False,False),
    ('material','invented',False,True),
    ('fact','itinerary',False,True),
    ('material',None,True,False),
    ('fact',None,True,True),
])
def test_material_handling_does_not_forge_factual_coverage(monkeypatch,request_kind,asset,receipt,missing):
    q=dict(request_quote='資料',request_kind=request_kind,covered=True,
           answer_segment_ids=[],answer_asset_ids=[asset] if asset else [])
    value=audit(q)
    value['material_request_checks']=[dict(kind='altitude',quote='資料')]
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(deepcopy(value),[],''))
    result=verify_scope(dict(customer_message='資料',
        proposed_body='這份資料目前無法完整提供，我會請顧問協助補齊。' if receipt else '介紹。',
        selected_asset_ids=['itinerary'],ordered_delivery_sections=[{'text':'介紹。'}],
        v2_events=[dict(type='material_requested',material_kind='altitude')],
        planned_system_action={'handoff_reason':'requested_material_unavailable'} if receipt else {}))[0]
    assert bool(result.get('missing_answers')) is missing
    assert bool(result['event_errors']) is (request_kind=='fact')
    if receipt and request_kind=='material':
        assert result['question_checks'][0]['response_status']=='pending_material'


def test_bad_fact_prefix_removal_keeps_necessary_unknown_task(monkeypatch):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import revise_copy
    decision=EvaluationDecision('reply','peach_9d','other',
        reply='6位符合優惠，具體折扣金額請顧問核對。')
    verification=FactVerification(False,unsupported_claims=['6位符合優惠'])
    calls=[]
    def repair(**kw):
        calls.append(kw['node'])
        return {'reply':'具體折扣金額請顧問核對。'},[],''
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',repair)
    result,_,_=revise_copy({},decision,verification,set())
    assert result['reply']=='具體折扣金額請顧問核對。'
    # The unsupported leading comma clause may govern the remainder. Use the
    # audited generation path rather than mechanically cutting an unproved tail.
    assert calls==['v2_reply_revision']


@pytest.mark.parametrize('bound,selected,expected',[
    ('peach_9d_2027','','peach_9d_2027'),
    ('','',''),
    ('peach_9d_2027','peach_11d_2027','peach_11d_2027'),
])
def test_empty_draft_does_not_erase_route_binding(bound,selected,expected):
    import json
    from app.reception_v2.runtime import _validated_decision
    raw=dict(action='reply',reply='可以先了解。',route_variant=selected,v2_events=[])
    result=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':'reply','route_variant':bound,'customer_text':'可以先了解嗎？'})
    assert result.route_variant==expected


@pytest.mark.parametrize('module',['silence_touch','wakeup'])
def test_scheduler_discards_draft_customer_events_and_profile(module):
    import json
    from app.reception_v2.runtime import _validated_decision
    raw=dict(action='reply',reply='補充一項行程資訊。',
        slots={'party_size':'8'},slot_evidence={'party_size':'我們8位'},
        v2_events=[{'type':'profile_updated','quote':'我們8位'},
                   {'type':'contact_agreed','quote':'明天聯絡我'}])
    result=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':module,'route_variant':'peach_9d_2027','customer_text':''})
    assert result.v2_events==[] and result.slots=={} and result.slot_evidence=={}


def test_internal_product_approval_does_not_ban_permit_approval():
    from app.advisor_voice import v2_internal_copy_violation
    assert v2_internal_copy_violation('目前批准的路線只在春季出發。')
    assert not v2_internal_copy_violation('入藏函需經審批通過。')


def test_partial_deletion_must_not_spend_a_round_with_unfixed_mixed_task(monkeypatch):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import revise_copy
    decision=EvaluationDecision('handoff','peach_9d','other',
        reply='請顧問核對折扣和門檻。單住另補2600元。',
        handoff_reason='knowledge_confirmation_required')
    audit_result=FactVerification(True,scope_check={
        'unwanted_parts':['單住另補2600元。'],
        'consultant_tasks':['核對6人折扣金額'],
        'task_scope_checks':[{'needed':True,'has_unasked_details':True}],
        'task_scope_errors':['客户未请求的核对事项：門檻']})
    def model(**kw):
        assert kw['input_data']['scope_check']['consultant_tasks']==['核對6人折扣金額']
        return kw['parser']({'reply':'6人的具體折扣金額，我會請顧問核對。','evidence_refs':[]}),[],''
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',model)
    result,_,_=revise_copy({},decision,audit_result,set())
    assert '門檻' not in result['reply'] and '2600' not in result['reply']


def test_unneeded_knowledge_handoff_allows_copy_repair_before_reconciliation():
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import can_revise_copy
    decision=EvaluationDecision('handoff','peach_9d','other',reply='顧問核對。',
        handoff_reason='knowledge_confirmation_required')
    result=FactVerification(True,scope_check={'consultant_tasks':[]},contract_violations=[
        '客户没有需要旅行顾问实际核对的事项，删除额外核对承诺并保持action=reply、handoff_reason=null；不要把普通已知答案转人工。'])
    assert can_revise_copy(decision,result)
    decision.handoff_reason='explicit_human_request'
    assert not can_revise_copy(decision,result)


@pytest.mark.parametrize('valid',[True,False])
def test_reasoning_exhaustion_retries_same_proof_within_original_budget(monkeypatch,valid):
    import time
    from types import SimpleNamespace
    from app import model_gateway as gateway
    from app.reception_v2.budget import deadline
    from app.deepseek_evaluation import EvaluationCallError
    requests=[]
    monkeypatch.setattr(gateway.settings,'deepseek_api_key','isolated-test')
    def post(payload,timeout):
        requests.append(deepcopy(payload))
        assert 0 < timeout <= 10
        if len(requests)==1: raise ValueError('deepseek_output_token_limit')
        return SimpleNamespace(raise_for_status=lambda:None,extensions={},json=lambda:{
            'choices':[{'message':{'content':'{"proof": true}'},'finish_reason':'stop'}]})
    monkeypatch.setattr(gateway,'_post_with_deadline',post)
    def parse(value):
        if not valid: raise ValueError('invalid_proof')
        return value
    token=deadline.set(time.monotonic()+10)
    try:
        kwargs=dict(node='isolated-proof',system_prompt='same strict proof',input_data={'fact':'evidence'},
            parser=parse,max_tokens=3000,repair_prompt='same schema',reasoning_effort='low',
            reasoning_fallback_tokens=1800)
        if valid:
            result,logs,_=gateway.call_json_node(**kwargs)
            assert result=={'proof':True} and len(logs)==2
        else:
            with pytest.raises(EvaluationCallError): gateway.call_json_node(**kwargs)
        assert len(requests)==2
        assert requests[0]['messages']==requests[1]['messages']
        assert requests[1]['thinking']=={'type':'disabled'} and requests[1]['max_tokens']==1800
    finally:
        deadline.reset(token)


def test_task_proof_reuse_is_exact_and_confined_to_one_turn(monkeypatch):
    from app.reception_v2.budget import bounded_turn,task_proofs
    from app.reception_v2.task_scope import verify_task_scope
    calls=[]
    def model(**kw):
        calls.append(kw['input_data'])
        return [{'index':0,'needed':False,'remove_from_reply':True}],[],str(len(calls))
    monkeypatch.setattr('app.reception_v2.task_scope.call_json_node',model)
    @bounded_turn
    def turn():
        data={'customer_message':'只問小團人數','request_reference_facts':[{'id':'group','text':'4至10人'}]}
        first=verify_task_scope(data,['查餘位'])
        first[0][0]['needed']=True  # Callers cannot mutate cached evidence.
        second=verify_task_scope(data,['查餘位'])
        assert second[0][0]['needed'] is False
        assert second[1][0]['status']=='cached'
        verify_task_scope(data,['查日期'])
        verify_task_scope({**data,'customer_message':'請查餘位'},['查餘位'])
        verify_task_scope({**data,'request_reference_facts':[]},['查餘位'])
    turn()
    assert len(calls)==4 and task_proofs.get() is None
    turn()
    assert len(calls)==8 and task_proofs.get() is None


@pytest.mark.parametrize('available',[False,True])
def test_guide_delivery_preserves_separate_factual_answer(available):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reception_v2.runtime import _enforce_delivery_contract
    from app.route_packages import ROUTES
    route='peach_9d_2027'
    guide_key=ROUTES[route]['policies']['post_capture_material_group']
    assets=ROUTES[route]['groups'][guide_key]['assets']
    body='小費建議每人每天30元人民幣，團費不包含小費。'
    decision=EvaluationDecision('reply','peach_9d','other',route_variant=route,reply=body,
        reply_body=body,evidence_refs=['route.shared.tips'],v2_events=[
            {'type':'question','quote':'小費多少'},
            {'type':'material_requested','quote':'高反PDF','material_kind':'altitude'}])
    context={'module':'reply','available_materials':[{'key':key} for key in assets] if available else []}
    _enforce_delivery_contract(context,decision)
    if available:
        assert decision.v2_delivery_sections[0]['text']==body
        assert decision.v2_delivery_sections[0]['answers_customer_question'] is True
        assert decision.v2_delivery_sections[1]['asset_keys']==assets
    else:
        assert decision.reply.startswith(body)
        assert decision.reply.endswith('這份資料目前無法完整提供，我會請顧問協助補齊。')
        assert decision.material_keys==[] and decision.covered_content_groups==[]
        first=decision.reply
        _enforce_delivery_contract(context,decision)
        assert decision.reply==first
    assert 'route.shared.tips' in decision.evidence_refs


@pytest.mark.parametrize('compound',[False,True])
def test_material_event_omission_is_repaired_from_current_semantic_evidence(compound):
    from types import SimpleNamespace
    from app.reception_v2.state_revision import revise_grounded_state
    quote='請給我高反PDF'
    events=[{'type':'question','quote':quote}]
    questions=[{'request_kind':'material','request_quote':quote}]
    if compound:
        events.append({'type':'question','quote':'小費多少'})
        questions.append({'request_kind':'fact','request_quote':'小費多少'})
    raw={'v2_events':events,'reply':'既有答案','slots':{},'slot_evidence':{}}
    scope={'material_request_checks':[{'kind':'altitude','quote':quote}],
        'question_checks':questions,'event_errors':['missing material event'],
        'profile_update_checks':[{'field':'party_size','quote':'我們6位','value':'6','persisted_correctly':False}]}
    result=revise_grounded_state({'customer_text':quote+'，我們6位，小費多少'},raw,None,SimpleNamespace(scope_check=scope))
    assert result['slots']=={'party_size':'6'}
    assert any(e.get('material_kind')=='altitude' for e in result['v2_events'])
    assert any(e.get('type')=='question' for e in result['v2_events']) is compound
    assert result['reply']==raw['reply'] and raw['slots']=={}


def test_material_repair_cannot_use_historical_request():
    from types import SimpleNamespace
    from app.reception_v2.state_revision import revise_grounded_state
    assert revise_grounded_state({'customer_text':'小費多少'}, {'v2_events':[]},SimpleNamespace(v2_events=[]),
        SimpleNamespace(scope_check={'material_request_checks':[{'kind':'altitude','quote':'給我PDF'}]})) is None


def test_contact_receipt_does_not_overwrite_current_question():
    from app.deepseek_evaluation import EvaluationDecision
    from app.reception_v2.runtime import _enforce_delivery_contract
    body='小費建議每人每天30元人民幣。'
    decision=EvaluationDecision('handoff','peach_9d','other',route_variant='peach_9d_2027',
        reply=body,reply_body=body,lead_action='captured',contact_values={'wechat':'test_traveller26'},
        evidence_refs=['route.shared.tips'],v2_events=[{'type':'human_requested','quote':'請顧問聯絡我'},
            {'type':'question','quote':'小費多少'}])
    _enforce_delivery_contract({'module':'reply','available_materials':[]},decision)
    assert decision.reply.startswith(body)
    assert decision.reply.endswith('聯絡方式已收到，我會請顧問接續協助，並補給您高原行前資料。')
    assert 'route.shared.tips' in decision.evidence_refs and decision.handoff_reason=='lead_captured'
    assert 'pending_material:altitude_guide' in decision.safety_flags


@pytest.mark.parametrize('refs,changed',[(['reply_0'],True),(['invented'],False)])
def test_service_copy_keeps_actual_question_spans_and_one_receipt(refs,changed):
    from types import SimpleNamespace
    from app.reception_v2.state_revision import revise_grounded_state
    receipt='聯絡方式已收到，我會請顧問接續協助，並補給您高原行前資料。'
    body='小費建議每人每天30元人民幣。您的微信收到，我會請顧問聯絡。\n\n'+receipt
    decision=SimpleNamespace(reply=body,reply_body=body,handoff_reason='lead_captured',v2_events=[])
    raw={'reply':body,'reply_body':body,'v2_events':[],'evidence_refs':['route.shared.tips']}
    scope={'question_checks':[{'request_kind':'fact','request_quote':'小費多少','covered':True,'answer_segment_ids':refs}]}
    result=revise_grounded_state({'customer_text':'微信已提供，小費多少'},raw,decision,SimpleNamespace(scope_check=scope))
    assert (result is not None) is changed
    if changed:
        assert result['reply']=='小費建議每人每天30元人民幣。\n\n'+receipt
        assert result['evidence_refs']==raw['evidence_refs']


def test_composite_material_repair_restores_omitted_fact_event_without_rewriting():
    from types import SimpleNamespace
    from app.reception_v2.state_revision import revise_grounded_state
    raw={'reply':'小費每人每天30元人民幣。','v2_events':[
        {'type':'material_requested','quote':'給我PDF','material_kind':'altitude'}]}
    scope={'material_request_checks':[{'kind':'altitude','quote':'給我PDF'}],
        'question_checks':[{'request_kind':'fact','request_quote':'小費多少','covered':False}],
        'missing_answers':['小費未回答']}
    result=revise_grounded_state({'customer_text':'給我PDF，小費多少'},raw,None,SimpleNamespace(scope_check=scope))
    assert result['reply']==raw['reply']
    assert result['v2_events'][-1]=={'type':'question','quote':'小費多少','topic':'current_question'}


def test_missing_fact_with_valid_pending_material_needs_only_copy():
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import can_revise_copy
    decision=EvaluationDecision('handoff','peach_9d','other',reply='資料待補。',
        handoff_reason='requested_material_unavailable',v2_events=[{'type':'question','quote':'小費多少'}])
    audit_result=FactVerification(True,scope_check={'missing_answers':['小費未回答'],
        'question_checks':[{'request_kind':'fact','request_quote':'小費多少'}]})
    assert can_revise_copy(decision,audit_result)
    decision.v2_events=[]
    assert not can_revise_copy(decision,audit_result)
