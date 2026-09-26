import json
import pytest
from app.deepseek_evaluation import EvaluationDecision, EvaluationCallError
from app.reply_fact_verification import FactVerification


@pytest.mark.parametrize('body,removable',[
    ('入藏函在成都交付。所有飯店都同等級。',True),
    ('所有飯店都同等級。',False),
])
def test_final_prune_never_generates_or_returns_an_empty_answer(monkeypatch,body,removable):
    from app.reception_v2.reply_revision import prune_copy
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('No generative call allowed'))
    d=EvaluationDecision('reply','peach_9d','other',reply=body,evidence_refs=['service.peach_permit'])
    a=FactVerification(False,unsupported_claims=['所有飯店都同等級。'],claim_checks=[
        {'claim':'入藏函在成都交付','supported':True,'evidence':'service.peach_permit'}])
    result=prune_copy({},d,a,{'service.peach_permit'})
    if removable:assert result[0]['reply']=='入藏函在成都交付。'
    else:assert result is None


@pytest.mark.parametrize('can_prune',[True,False])
def test_two_generations_then_only_audited_deterministic_cleanup(monkeypatch,can_prune):
    from app.reception_v2 import runtime
    monkeypatch.setattr(runtime.settings,'deepseek_api_key','test')
    body=('入藏函在成都交付。' if can_prune else '')+'所有飯店都同等級。'
    original={'action':'reply','route_variant':'peach_9d_2027','reply':body,'v2_events':[],
              'evidence_refs':['service.peach_permit']}
    monkeypatch.setattr(runtime,'_call',lambda *a:({'content':json.dumps(original)},{'duration_ms':1}))
    audits=[]
    def verify(context,decision):
        audits.append(decision.reply)
        if decision.reply=='入藏函在成都交付。':return FactVerification(True),[],''
        return FactVerification(False,unsupported_claims=['所有飯店都同等級。'],claim_checks=[
            {'claim':'入藏函在成都交付','supported':True,'evidence':'service.peach_permit'}]),[],''
    monkeypatch.setattr(runtime,'_verify',verify)
    revisions=[]
    def revise(*args):
        revisions.append(1)
        return {'reply':body,'evidence_refs':['service.peach_permit']},[],''
    monkeypatch.setattr('app.reception_v2.reply_revision.revise_copy',revise)
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('Third generation forbidden'))
    context={'module':'reply','customer_text':'入藏函在哪拿？','route_variant':'peach_9d_2027'}
    if can_prune:
        decision,*_=runtime.run_v2_agent(context)
        assert decision.reply=='入藏函在成都交付。' and len(audits)==4
    else:
        with pytest.raises(EvaluationCallError,match='verification_failed'):runtime.run_v2_agent(context)
        assert len(audits)==3
    assert len(revisions)==2


def test_proactive_fact_proof_retains_approved_route_context_without_widening_new_value(monkeypatch):
    import app.reply_fact_verification as proof
    from app.reception_v2.runtime import _verify
    proof._VERIFIER_CACHE.clear()
    captured=[]
    def model(**kw):
        captured.append({f['id'] for f in kw['input_data']['allowed_facts']})
        return FactVerification(True),[],''
    monkeypatch.setattr(proof,'call_json_node',model)
    monkeypatch.setattr('app.reception_v2.reply_scope.verify_scope',lambda data:({
        'unwanted_parts':[],'event_errors':[],'missing_answers':[],'consultant_tasks':[]},[],''))
    candidate='route.shared.peach_highlights'
    context={'engine_version':'v2','module':'silence_touch','v2_available_fact_ids':[candidate],
             'v2_proactive_candidate_fact_ids':[candidate]}
    decision=EvaluationDecision('reply','peach_9d','other',route_variant='peach_9d_2027',
        reply='沿線有帕邦喀寺和秀巴古堡。',evidence_refs=[candidate])
    _verify(context,decision)
    assert 'route.9.scope' in captured[0] and 'route.11.rongbuk' not in captured[0]
    assert context['v2_available_fact_ids']==[candidate] and decision.evidence_refs==[candidate]


def test_final_fact_recheck_has_bounded_reasoning_and_does_not_approve_rejection(monkeypatch):
    import app.reply_fact_verification as proof
    from app.reception_v2.runtime import _verify
    proof._VERIFIER_CACHE.clear()
    calls=[]
    def model(**kwargs):
        calls.append(kwargs)
        return FactVerification(False,unsupported_claims=['unsupported']),[],''
    monkeypatch.setattr(proof,'call_json_node',model)
    monkeypatch.setattr('app.reception_v2.reply_scope.verify_scope',lambda data:({
        'unwanted_parts':[],'event_errors':[],'missing_answers':[],'consultant_tasks':[]},[],''))
    decision=EvaluationDecision('reply','peach_9d','other',reply='unsupported',route_variant='peach_9d_2027')
    result,*_=_verify({'engine_version':'v2','customer_text':'test','v2_final_fact_recheck':True},decision)
    assert result.supported is False
    assert len(calls)==2 and calls[1]['node']=='v2_fact_recheck'
    assert calls[1]['reasoning_timeout_seconds']==6.0
    assert calls[1]['reasoning_fallback_tokens']==1800 and calls[1]['max_tokens']==3000


@pytest.mark.parametrize('body,expected',[
 ('多人優惠多少要再幫您核對。',True),
 ('會替您確認這個日期的餘位。',True),
 ('我不幫您判斷用藥劑量。',False),
 ('不用再幫您核對優惠。',False),
 ('不幫您核對優惠。',False),
 ('6人小團9980元起。',False),
])
def test_implicit_service_actor_is_a_candidate_not_an_intent(body,expected):
    from app.reception_v2.reply_scope import checking_commitment
    assert checking_commitment(body) is expected


def test_caption_provenance_references_real_segments_without_an_extra_message(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    captured=[]
    def model(**kw):
        captured.append(kw['input_data'])
        return {'event_errors':[],'event_error_checks':[],'question_checks':[],
            'contact_refusals':[],'material_request_checks':[],'consultant_task_checks':[]},[],''
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    verify_scope({'customer_message':'看看車圖，也說小費。','proposed_body':'小費30元。車上有氧氣。這是車圖。',
        'compiled_material_captions':[{'kind':'vehicle','text':'車上有氧氣。這是車圖。'}]})
    packet=captured[0]
    assert 'compiled_material_captions' not in packet
    assert packet['compiled_caption_segment_ids']==['reply_1','reply_2']
    assert len(packet['actual_answer_segments'])==3


@pytest.mark.parametrize('reference,duplicate,valid',[
 ('reply_0',False,False),('missing',False,False),(None,False,False),
 ('reply_1',True,True),('history_0',False,True),
])
def test_repetition_requires_a_distinct_actual_occurrence(monkeypatch,reference,duplicate,valid):
    from app.reception_v2.reply_scope import verify_scope
    body='車上有氧氣。'+('車內配備氧氣。' if duplicate else '')
    audit={'current_request':'車圖','required_answer':'車圖','event_error_checks':[],
        'question_checks':[],'material_request_checks':[],'profile_update_checks':[],
        'traveler_age_checks':[],'contact_refusals':[],'requested_material_kinds':[],
        'consultant_task_checks':[],'unwanted_part_checks':[{'kind':'repeat_content',
            'quote':'車上有氧氣。','reason':'重複','repeat_of_segment_id':reference}]}
    def model(**kw):
        if valid:return kw['parser'](audit),[],''
        with pytest.raises(ValueError,match='repeat_evidence_invalid'):
            kw['parser'](audit)
        audit['unwanted_part_checks']=[]
        return kw['parser'](audit),[],''
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    history=[{'role':'assistant','content':'車內配備氧氣。'}] if reference=='history_0' else []
    result=verify_scope({'customer_message':'車圖','proposed_body':body,'recent_conversation':history})[0]
    assert bool(result['unwanted_parts']) is valid


@pytest.mark.parametrize('body,duplicate',[
 ('目前4至6人配置是2025年9座VIP航空座椅車，配有彌散式供氧和緊急醫療氧氣鋼瓶。',True),
 ('小費建議導遊司機每人每天30元人民幣，團費不包含小費。',False),
 ('目前8至10人配置是2025年9座VIP航空座椅車，配有彌散式供氧和緊急醫療氧氣鋼瓶。',False),
 ('目前4至6人配置是2025年9座VIP航空座椅車，不配有彌散式供氧和緊急醫療氧氣鋼瓶。',False),
])
def test_real_long_caption_duplicate_preserves_different_conditions(monkeypatch,body,duplicate):
    from app.reception_v2.reply_scope import verify_scope
    caption='4至6人用車安排2025年9座VIP航空座椅車，配有彌散式供氧及緊急醫療氧氣鋼瓶。'
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:({
        'event_errors':[],'event_error_checks':[],'question_checks':[],
        'contact_refusals':[],'material_request_checks':[],'consultant_task_checks':[]},[],''))
    result=verify_scope({'customer_message':'車圖','proposed_body':body+caption,
        'compiled_material_captions':[{'kind':'vehicle','text':caption}]})[0]
    assert (body in result.get('unwanted_parts',[])) is duplicate
