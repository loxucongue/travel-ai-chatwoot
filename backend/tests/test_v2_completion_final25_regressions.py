from copy import deepcopy
import pytest
import json


@pytest.mark.parametrize('reply,accepted',[
    ('機票不在包含範圍內，需要另外處理。', True),
    ('機票不在包含項目裡。', True),
    ('機票不包含在9980裡。', True),
    ('機票包含在9980裡。', False),
    ('機票是否包含需要確認。', False),
])
def test_flight_exclusion_acceptance_supports_equivalent_wording(reply, accepted):
    import re
    from tests.test_v2_service_acceptance import CASES
    pattern = next(case[4][1] for case in CASES if case[0] == 'flight')
    assert bool(re.search(pattern, reply)) is accepted


def test_customer_turn_cannot_skip_semantic_and_refusal_audit():
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'no_action','intent':'other','reply':'好的，不再打擾。','v2_events':[]}
    with pytest.raises(ValueError,match='v2_customer_reply_required'):
        _validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'不要再推銷'})
    d=_validated_decision({'content':json.dumps(raw)},set(),set(),{'module':'silence_touch','customer_text':'不要再推銷'})
    assert d.action=='no_action'


@pytest.mark.parametrize('grounded,permanent,accepted',[(True,False,True),(False,False,False),(True,True,False)])
def test_temporary_appointment_receipt_requires_current_grounded_state(monkeypatch,grounded,permanent,accepted):
    import app.reply_fact_verification as proof
    from app.reception_v2.runtime import _verify
    from app.deepseek_evaluation import EvaluationDecision
    from app.customer_contact_policy import V2_APPOINTMENT_RECEIPT
    proof._VERIFIER_CACHE.clear()
    claim='永遠不再主動聯繫您' if permanent else '這段時間先不打擾您'
    def model(**kw):
        return kw['parser']({'unsupported_claims':[claim],'claim_checks':[
            {'claim':claim,'supported':False,'evidence':'missing','reason':'Model missed execution state'}]}),[],''
    monkeypatch.setattr(proof,'call_json_node',model)
    monkeypatch.setattr('app.reception_v2.reply_scope.verify_scope',lambda data:({'unwanted_parts':[],
        'event_errors':[],'missing_answers':[],'consultant_tasks':[]},[],''))
    d=EvaluationDecision('handoff','peach_9d','other',reply=claim if permanent else V2_APPOINTMENT_RECEIPT,
        handoff_reason='customer_contact_outside_window',v2_events=[{'type':'contact_agreed',
            'quote':'後天早上10點' if grounded else '下週','contact_at':'2026-09-22T10:00:00+08:00'}])
    result,_,_=_verify({'engine_version':'v2','module':'reply','customer_text':'後天早上10點',
        'now':'2026-09-20T02:00:00+00:00'},d)
    assert result.supported is accepted


@pytest.mark.parametrize('mixed',[False,True])
def test_pending_file_does_not_require_unsolicited_essay_or_cover_real_question(monkeypatch,mixed):
    from app.reception_v2.reply_scope import verify_scope
    current='請給我高反PDF'+('，高反有什麼症狀？' if mixed else '')
    questions=[{'request_quote':'請給我高反PDF','request_kind':'material','answer_kind':'attachment',
        'answer_segment_ids':[],'answer_asset_ids':[],'covered':False}]
    if mixed:questions.append({'request_quote':'高反有什麼症狀','request_kind':'fact','answer_kind':'text',
        'answer_segment_ids':[],'answer_asset_ids':[],'covered':False})
    audit={'event_error_checks':[],'question_checks':questions,'contact_refusals':[],
        'material_request_checks':[{'kind':'altitude','quote':'請給我高反PDF'}],
        'requested_material_kinds':[],'consultant_task_checks':[],
        'missing_answers':['高反症狀介紹'],'unwanted_parts':[],'unwanted_part_checks':[]}
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(deepcopy(audit),[],''))
    result,_,_=verify_scope({'customer_message':current,
        'proposed_body':'這份資料目前無法完整提供，我會請顧問協助補齊。',
        'planned_system_action':{'handoff_reason':'requested_material_unavailable'},
        'v2_events':[{'type':'material_requested','material_kind':'altitude','quote':'請給我高反PDF'}]})
    assert bool(result['missing_answers']) is mixed
    assert result['question_checks'][0]['response_status']=='pending_material'


@pytest.mark.parametrize('checks,tasks,reason,event,changed',[
    ([{'needed':False}],[],'knowledge_confirmation_required',None,True),
    ([],[],'knowledge_confirmation_required',None,False),
    ([{'needed':True}],['ticket price'],'knowledge_confirmation_required',None,False),
    ([{'needed':False},{'needed':True}],['permit transfer'],'knowledge_confirmation_required',None,False),
    ([{'needed':False}],[],'explicit_human_request',None,False),
    ([{'needed':False}],[],'knowledge_confirmation_required','human_requested',False),
])
def test_rejected_task_copy_repair_also_reconciles_action(checks,tasks,reason,event,changed):
    from app.reception_v2.state_revision import rejected_task_action_patch
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    d=EvaluationDecision('handoff','peach_9d','other',reply='回程從拉薩搭鐵路出藏。',
        handoff_reason=reason,v2_events=[{'type':event}] if event else [])
    a=FactVerification(True,scope_check={'task_scope_checks':checks,'consultant_tasks':tasks})
    patch=rejected_task_action_patch(d,a)
    assert bool(patch) is changed
    if changed:
        assert patch['action']=='reply' and patch['handoff_reason'] is None
        assert d.action=='handoff'  # Candidate edit only; no mutation or delivery.


def test_question_repair_cannot_recreate_state_or_approved_references(monkeypatch):
    from app.reception_v2.reply_shape import repair_question_shape
    raw={'action':'reply','route_variant':'peach_9d_2027','slots':{'party_size':'2'},
        'v2_events':[{'type':'route_selected','quote':'選桃花9日'}],
        'evidence_refs':['route.9.overview'],'material_keys':['routes12-9d-itinerary'],
        'reply':'已記下9日。幾位？何時？','follow_up_question':'幾位？何時？',
        'follow_up_type':'collect_need','follow_up_field':'party_size'}
    saved=deepcopy(raw)
    def model(**kw):
        assert kw['node']=='v2_reply_shape_repair'
        return kw['parser']({'reply':'好喔，已記下桃花9日。','follow_up_question':'',
            'slots':{'party_size':'8'},'action':'handoff'}),[],''
    monkeypatch.setattr('app.reception_v2.reply_shape.call_json_node',model)
    revised,_,_=repair_question_shape(raw,{'customer_text':'選桃花9日'})
    assert raw==saved
    for key in ['action','route_variant','slots','v2_events','evidence_refs','material_keys']:
        assert revised[key]==saved[key]
    assert revised['follow_up_type']=='none' and revised['follow_up_field']==''
    assert revised['reply_body']==revised['reply']


@pytest.mark.parametrize('reply,question',[('幾位？何時？',''),('',''),('幾位？','何時？')])
def test_shape_repair_rejects_ambiguous_or_empty_result(monkeypatch,reply,question):
    from app.reception_v2.reply_shape import repair_question_shape
    monkeypatch.setattr('app.reception_v2.reply_shape.call_json_node',
        lambda **kw:(kw['parser']({'reply':reply,'follow_up_question':question}),[],''))
    with pytest.raises(ValueError,match='v2_reply_shape_invalid'):
        repair_question_shape({'reply':'幾位？何時？'}, {})
