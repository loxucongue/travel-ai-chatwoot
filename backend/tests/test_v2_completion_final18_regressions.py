"""Request semantics stay separate from actual delivery evidence."""
from copy import deepcopy
import pytest


def audit(question):
    return dict(current_request='資料', required_answer='實際交付',
        question_checks=[question], event_error_checks=[], material_request_checks=[],
        profile_update_checks=[], traveler_age_checks=[], contact_refusals=[],
        unwanted_part_checks=[], requested_material_kinds=[], consultant_task_checks=[])


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
