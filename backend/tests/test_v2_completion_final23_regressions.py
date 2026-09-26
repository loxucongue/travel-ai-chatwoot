import pytest
import json
from copy import deepcopy


@pytest.mark.parametrize('requested,exact,protected',[(True,True,True),(False,True,False),(True,False,False)])
def test_only_compiled_full_intro_clarification_is_protected(monkeypatch,requested,exact,protected):
    from app.reception_v2.reply_scope import verify_scope
    question='這次幾位同行呢？'
    audit={'event_error_checks':[],'question_checks':[],'contact_refusals':[],
        'material_request_checks':[{'kind':'full_introduction','quote':'完整介紹'}] if requested else [],
        'requested_material_kinds':['full_introduction'] if requested else [],
        'consultant_task_checks':[],'unwanted_parts':[question],
        'unwanted_part_checks':[{'quote':question,'kind':'unnecessary_question','reason':'test'}]}
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(deepcopy(audit),[],''))
    result,*_=verify_scope({'customer_message':'完整介紹' if requested else '你好',
        'proposed_body':'這是介紹。','planned_follow_up':question,
        'v2_events':[{'type':'material_requested','material_kind':'full_introduction'}] if requested else [],
        'server_clarification':{'question':question if exact else '不同問題','field':'party_size',
            'reason':'requested_full_intro_missing_party'}})
    assert (question not in result['unwanted_parts']) is protected


def test_v2_parser_does_not_inject_legacy_route_citations():
    from app.deepseek_evaluation import EvaluationDecision
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','intent':'other','branch':'unclassified','reply':'我們有桃花9日和桃花加珠峰11日行程。',
         'v2_events':[],'evidence_refs':[],'reply_options':['桃花9日','桃花+珠峰11日']}
    assert EvaluationDecision.parse(raw).evidence_refs
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'你好'})
    assert decision.evidence_refs==[]
    replay={**raw,'reply':decision.reply,'evidence_refs':decision.evidence_refs}
    assert _validated_decision({'content':json.dumps(replay)},set(),set(),{'customer_text':'你好'}).reply
    with pytest.raises(ValueError,match='unknown_evidence_reference'):
        _validated_decision({'content':json.dumps({**raw,'evidence_refs':['route.9.overview']})},set(),set(),{})


def test_prune_does_not_leave_a_negation_without_its_oxygen_subject(monkeypatch):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import prune_copy
    d=EvaluationDecision('reply','peach_9d','other',
        reply='車上和住宿的供氧是備著讓您不舒服時使用，不代表有心臟疾病就一定不會有高原反應。',
        evidence_refs=['service.safety'])
    a=FactVerification(False,unsupported_claims=['車上和住宿的供氧是備著讓您不舒服時使用'],
        claim_checks=[{'claim':'不代表有心臟疾病就一定不會有高原反應','supported':True,'evidence':'service.safety'}])
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('Prune cannot generate'))
    assert prune_copy({},d,a,{'service.safety'}) is None
@pytest.mark.parametrize('still_missing',[True,False])
def test_event_disagreement_rechecks_action_and_preserves_actual_errors(monkeypatch,still_missing):
    from app.reception_v2.reply_scope import verify_scope
    base={'current_request':'在哪集合？','required_answer':'集合地点','event_error_checks':[],
        'question_checks':[{'request_quote':'在哪集合？','request_kind':'fact','answer_kind':'text',
            'answer_segment_ids':['reply_0'],'answer_asset_ids':[],'covered':True}],
        'material_request_checks':[],'profile_update_checks':[],'traveler_age_checks':[],
        'contact_refusals':[],'requested_material_kinds':[],'consultant_task_checks':[],
        'unwanted_part_checks':[],'missing_answers':[],
        'event_error_checks':[{'quote':'在哪集合？','reason':'wrong event'}]}
    calls=[]
    def model(**kw):
        calls.append(kw)
        value=dict(base)
        if len(calls)==2:value['event_error_checks']=[{'quote':'在哪集合？','reason':'wrong event'}] if still_missing else []
        return kw['parser'](value),[{'node':kw['node']}],str(len(calls))
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    result,logs,_=verify_scope({'customer_message':'在哪集合？','proposed_body':'在林芝接機。'})
    assert bool(result['event_errors']) is still_missing
    assert len(calls)==2 and len(logs)==2
    assert calls[1]['node']=='v2_scope_coverage_recheck'
    assert calls[1].get('reasoning_effort') is None
    assert calls[1]['max_tokens']==1800
    assert calls[1]['input_data']['actual_answer_segments']==calls[0]['input_data']['actual_answer_segments']


@pytest.mark.parametrize('whole', [False, True])
def test_partial_repeat_can_repair_copy_but_full_repeat_requires_planning(whole):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import can_revise_copy
    repeated='珠峰住絨布旅館，房內有供氧和獨立衛浴。'
    reply=repeated if whole else '全程安排供氧住宿，波密和珠峰為希爾頓升級例外。'+repeated
    decision=EvaluationDecision('reply','peach_11d','other',reply=reply)
    audit=FactVerification(True,relevant=False,contract_violations=[repeated],scope_check={
        'unwanted_parts':[repeated],'unwanted_part_checks':[{'kind':'repeat_content','quote':repeated}]})
    assert can_revise_copy(decision,audit) is (not whole)


@pytest.mark.parametrize('quote,protected',[
 ('珠峰是希爾頓升級例外',True),
 ('珠峰住絨布旅館，房內有供氧和獨立衛浴',False),
 ('全程供氧住宿，波密和珠峰是希爾頓升級例外，其餘地區安排希爾頓飯店。',False),
])
def test_required_exception_is_not_removed_as_repetition(quote,protected):
    from app.reception_v2.reply_scope import preserve_required_hotel_exceptions
    body='全程供氧住宿，波密和珠峰是希爾頓升級例外，其餘地區安排希爾頓飯店。珠峰住絨布旅館，房內有供氧和獨立衛浴。'
    result={'unwanted_parts':[quote],'unwanted_part_checks':[{'quote':quote,'kind':'repeat_content'}]}
    preserve_required_hotel_exceptions({'event':'silence_due','selected_route':'peach_11d_2027','proposed_body':body if protected is False else body.split('。')[0]+'。'},result)
    assert (quote not in result['unwanted_parts']) is protected


def test_failed_optional_scope_adjudication_keeps_initial_rejection(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    from app.deepseek_evaluation import EvaluationCallError
    def model(**kw):
        if kw['node']=='v2_scope_coverage_recheck':
            raise EvaluationCallError('invalid_reference',[{'node':'v2_scope_coverage_recheck','status':'invalid_json'}],'failed')
        return {'event_error_checks':[],'question_checks':[],'contact_refusals':[],
            'requested_material_kinds':[],'consultant_task_checks':[],
            'unwanted_parts':['要幫您看11日嗎？'],
            'unwanted_part_checks':[{'quote':'要幫您看11日嗎？','kind':'unnecessary_question'}]},[], 'initial'
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    result,logs,digest=verify_scope({'customer_message':'有心臟病安全嗎？','proposed_body':'請醫師評估。要幫您看11日嗎？'})
    assert result['unwanted_parts']==['要幫您看11日嗎？']
    assert logs[-1]['status']=='initial_audit_retained'
    assert digest not in {'failed','initial'}
