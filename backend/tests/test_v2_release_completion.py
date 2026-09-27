"""Release regressions: customer constraints, route extension and delivery obligations."""
from copy import deepcopy
from dataclasses import asdict
import pytest
from sqlalchemy import select
from app.customer_contact_policy import contact_constraint
from app.deepseek_evaluation import EvaluationDecision
from app.models import ConversationJourney, ConversationState, HandoffTask
from app.reception_v2.events import validate_events, merge_events, answer_receipt
from app.reception_v2.runtime import _enforce_delivery_contract
from app.route_packages import ROUTES

NOW = '2026-09-20T02:00:00+00:00'
ROUTE = 'peach_9d_2027'


def test_exact_unasked_clause_removed_before_valid_medical_qualification(monkeypatch):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    unwanted='健康證明的開立方式與格式，建議由顧問幫您確認'
    reply='台灣旅客65至75歲需提交健康證明。'+unwanted+'；個人健康狀況請醫師評估。'
    decision=EvaluationDecision('reply','peach_9d','other',reply=reply,
        evidence_refs=['service.peach_age','service.safety'])
    audit=FactVerification(True,scope_check={'unwanted_parts':[unwanted]},claim_checks=[
        {'claim':'台灣旅客65至75歲需提交健康證明','supported':True,'evidence':'service.peach_age'},
        {'claim':'個人健康狀況請醫師評估','supported':True,'evidence':'service.safety'}])
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',
        lambda **kw:pytest.fail('Exact standalone clause needs no generative rewrite'))
    result,_,_=revise_copy({},decision,audit,set(decision.evidence_refs))
    assert result['reply']=='台灣旅客65至75歲需提交健康證明。個人健康狀況請醫師評估。'
    assert result['evidence_refs']==decision.evidence_refs


def test_overlapping_audit_spans_remove_outer_rejected_sentence_first(monkeypatch):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    tail='這部分要由顧問依您的證件核對。'
    outer='64歲材料要求未公布，不能說免交，'+tail
    decision=EvaluationDecision('reply','peach_9d','other',
        reply='64歲不是低於最低報名年齡。'+outer,evidence_refs=['service.peach_age'])
    audit=FactVerification(True,scope_check={'unwanted_parts':[tail,outer]},claim_checks=[
        {'claim':'64歲不是低於最低報名年齡','supported':True,'evidence':'service.peach_age'}])
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',
        lambda **kw:pytest.fail('Outer rejected sentence can be removed exactly'))
    result,_,_=revise_copy({},decision,audit,set(decision.evidence_refs))
    assert result['reply']=='64歲不是低於最低報名年齡。'


def event(kind, quote, **extra):
    return validate_events([{'type': kind, 'quote': quote, **extra}],
        {'customer_text': quote, 'now': NOW, 'source_message_id': 10})


def test_silence_does_not_replay_last_customer_question_as_new_input():
    from app.reception_v2.runtime import _messages
    from app.reception_v2.skill_registry import SkillRegistry
    messages = _messages({'module':'silence_touch','route_variant':ROUTE,'customer_text':'在哪集合？',
        'context_messages':[{'role':'customer','content':'在哪集合？'},
                            {'role':'assistant','content':'林芝接機。'}]}, SkillRegistry())
    assert len([m for m in messages if m['role'] == 'user' and m['content'] == '在哪集合？']) == 1


def test_hotel_request_preserves_separate_customer_question():
    decision = EvaluationDecision('reply','peach_9d','other',route_variant=ROUTE,
        reply='第一天林芝接機。',evidence_refs=['route.9.arrival'],
        v2_events=[{'type':'material_requested','material_kind':'hotel'},
                   {'type':'question','quote':'在哪集合？','source_message_id':10}])
    keys = ROUTES[ROUTE]['groups']['hotel_reference']['assets']
    _enforce_delivery_contract({'available_materials':[{'key':k} for k in keys]},decision)
    assert decision.v2_delivery_sections[0]['answers_customer_question']
    assert decision.v2_delivery_sections[0]['text'] == '第一天林芝接機。'
    assert decision.v2_delivery_sections[1]['asset_keys']


def test_full_intro_discards_unneeded_model_summary_before_single_message_limit():
    import json
    from app.reception_v2.runtime import _validated_decision
    raw = {'action':'reply','route_variant':ROUTE,'reply':'不應發送的冗長摘要。'*100,
        'v2_events':[{'type':'material_requested','quote':'完整介紹','material_kind':'full_introduction'}]}
    decision = _validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'完整介紹','module':'reply'})
    keys = {k for g in ROUTES[ROUTE]['groups'].values() for k in g['assets']}
    _enforce_delivery_contract({'available_materials':[{'key':k} for k in keys]},decision)
    assert len(decision.v2_delivery_sections) >= 5
    assert all('冗長摘要' not in s['text'] for s in decision.v2_delivery_sections)


def test_reactive_handoff_cannot_skip_answer_verification_with_empty_reply():
    import json
    from app.reception_v2.runtime import _validated_decision
    with pytest.raises(ValueError, match='v2_customer_reply_required'):
        _validated_decision({'content':json.dumps({'action':'handoff','reply':None,'v2_events':[],
            'handoff_reason':'knowledge_confirmation_required'})},set(),set(),{'module':'reply','customer_text':'需要什麼證明？'})


@pytest.mark.parametrize('facts_ok,scope_ok', [(True,True),(False,True),(True,False)])
@pytest.mark.parametrize('final_recheck',[False,True])
def test_parallel_verification_keeps_deadline_and_requires_both_checks(monkeypatch,facts_ok,scope_ok,final_recheck):
    import threading, time
    import app.reply_fact_verification as verifier
    import app.reception_v2.reply_scope as scope_module
    from app.reception_v2.budget import deadline
    from app.reception_v2.runtime import _verify
    barrier=threading.Barrier(2)
    expires=time.monotonic()+5
    fact_calls=[]
    def scope(data):
        assert deadline.get()==expires
        barrier.wait(timeout=2)
        return {'current_request':'test','unwanted_parts':[] if scope_ok else ['unasked extension'],
            'event_errors':[],'missing_answers':[],'consultant_tasks':[]},[{'node':'scope','duration_ms':1}],'scope'
    def fact(**kwargs):
        assert deadline.get()==expires
        fact_calls.append(kwargs['node'])
        if len(fact_calls)==1:
            assert 'reasoning_effort' not in kwargs
            barrier.wait(timeout=2)
        else:
            assert kwargs['reasoning_effort']=='low'
        return verifier.FactVerification(facts_ok,[] if facts_ok else ['unsupported']),[{'node':'facts','duration_ms':1}],'facts'
    monkeypatch.setattr(scope_module,'verify_scope',scope)
    monkeypatch.setattr(verifier,'call_json_node',fact)
    verifier._VERIFIER_CACHE.clear()
    token=deadline.set(expires)
    try:
        decision=EvaluationDecision('reply','peach_9d','other',reply='test',route_variant=ROUTE)
        result,logs,_=_verify({'engine_version':'v2','customer_text':'test','v2_final_fact_recheck':final_recheck},decision)
        assert (result.supported and result.relevant)==(facts_ok and scope_ok)
        assert len(logs)>=2
        assert len(fact_calls)==(2 if final_recheck and not facts_ok and scope_ok else 1)
    finally:
        deadline.reset(token)
        verifier._VERIFIER_CACHE.clear()


def test_scope_workers_close_transports_on_their_own_threads(monkeypatch):
    import threading
    from app.reception_v2.verification_pool import ScopeVerificationPool
    barrier=threading.Barrier(2)
    active,closed=set(),set()
    monkeypatch.setattr('app.deepseek_evaluation.close_deepseek_transport',lambda:closed.add(threading.get_ident()))
    pool=ScopeVerificationPool(workers=2)
    def work():
        active.add(threading.get_ident())
        barrier.wait(timeout=2)
    try:
        futures=[pool.submit(work) for _ in range(2)]
        for future in futures: future.result(timeout=3)
    finally:
        pool.close()
    assert len(active)==2 and closed==active


def test_incomplete_party_extraction_does_not_overwrite_date_acknowledgment():
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','route_variant':ROUTE,'reply':'6位，預計3月28日出發。',
         'slots':{'party_size':'6'},'slot_evidence':{'party_size':'6位'},
         'v2_events':[{'type':'profile_updated','quote':'我們6位，3月28日出發','topic':'party_size'}],
         'journey_stage':'value_building'}
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':'reply','customer_text':'我們6位，3月28日出發'})
    assert decision.reply==raw['reply']
    assert decision.slots=={'party_size':'6'}  # The semantic audit must repair the omitted date.


@pytest.mark.parametrize('slots,evidence,error',[
    ({'departure_date':'3月28日'},{'departure_date':'3月28日'},'v2_slot_unknown_field'),
])
def test_profile_parse_never_silently_discards_customer_updates(slots,evidence,error):
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','reply':'已記下日期。','slots':slots,'slot_evidence':evidence,'v2_events':[]}
    with pytest.raises(ValueError,match=error):
        _validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'3月28日出發'})


def test_followup_question_cannot_recommit_historical_profile_as_current_input():
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','reply':'依6人報價。','slots':{'party_size':'6'},
         'slot_evidence':{'party_size':'我們6位'},'v2_events':[{'type':'question','quote':'價格多少？'}]}
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':'reply','customer_text':'價格多少？','journey':{'customer_profile':{'party_size':'6'}}})
    assert decision.slots=={} and decision.slot_evidence=={}
    assert decision.reply=='依6人報價。'


@pytest.mark.parametrize('module',['silence_touch','wakeup'])
def test_scheduler_cannot_persist_model_invented_customer_profile(module):
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','reply':'補充一項資訊。','slots':{'party_size':'10','departure_date':'9月'},
         'slot_evidence':{'party_size':'10位'},'v2_events':[]}
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':module,'customer_text':'我們6位','journey':{'customer_profile':{'party_size':'6'}}})
    assert decision.slots=={} and decision.slot_evidence=={}


def test_scope_checks_new_profile_separately_from_old_profile_and_spoken_ack(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    def model(**kw):
        assert kw['input_data']['current_profile_updates']['slots']=={'party_size':'6'}
        return {'requested_material_kinds':[],'event_errors':[],'contact_refusals':[],
            'profile_update_checks':[{'field':'departure_window','quote':'3月28日','persisted_correctly':True}]},[],''
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    audit,_,_=verify_scope({'customer_message':'我們6位，3月28日出發',
        'proposed_body':'好的，6位，3月28日出發。','validated_customer_facts':{'departure_window':'3月25日'},
        'current_profile_updates':{'slots':{'party_size':'6'},'evidence':{'party_size':'6位'}}})
    assert any('departure_window' in error for error in audit['event_errors'])


@pytest.mark.parametrize('ack_only,event_kind,fields_complete,shortcut',[
    (True,'profile_updated',True,True),
    (False,'profile_updated',True,False),
    (True,'question',True,False),
    (True,'profile_updated',False,False),
])
def test_profile_ack_requires_independent_semantics_and_complete_fields(monkeypatch,ack_only,event_kind,fields_complete,shortcut):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    decision=EvaluationDecision('reply','peach_9d','other',reply='多餘報價。',route_variant=ROUTE,
        slots={'party_size':'6','departure_window':'3月28日'},
        v2_events=[{'type':event_kind,'quote':'我們6位，3月28日出發'}])
    if not fields_complete: decision.slots.pop('departure_window')
    audit=FactVerification(True,scope_check={'profile_ack_only':ack_only,
        'profile_update_checks':[{'field':f,'persisted_correctly':True} for f in ['party_size','departure_window']]})
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:({'reply':'model'},[{'node':'model'}],''))
    result,logs,_=revise_copy({},decision,audit,set())
    assert (logs[0]['node']=='v2_verified_profile_ack')==shortcut
    if shortcut:
        assert '6位' in result['reply'] and '3月28日' in result['reply']
        assert not result['evidence_refs']


def test_wording_revision_never_handles_event_or_operator_policy_errors():
    from app.reception_v2.reply_revision import can_revise_copy
    from app.reply_fact_verification import FactVerification
    decision=EvaluationDecision('reply','peach_9d','other',reply='text',route_variant=ROUTE)
    assert can_revise_copy(decision,FactVerification(False,['unsupported']))
    assert not can_revise_copy(decision,FactVerification(True,scope_check={'event_errors':['wrong event']}))
    assert not can_revise_copy(decision,FactVerification(True,contract_violations=['disabled route']))
    decision.action='handoff'
    decision.handoff_reason='knowledge_confirmation_required'
    assert can_revise_copy(decision,FactVerification(True,scope_check={
        'consultant_tasks':[],'unwanted_parts':['unneeded check']},contract_violations=['unneeded check']))


def test_scope_suggestions_cannot_override_rejected_facts_during_revision():
    from app.reception_v2.reply_revision import scope_revision_context
    from app.reply_fact_verification import FactVerification
    scope={'current_request':'戶外氧氣是否自備','required_answer':'一律不用自備',
        'revision_instructions':'保留不用自備','missing_answers':['不用自備'],
        'unwanted_parts':['住宿介紹'],'event_errors':[]}
    revised=scope_revision_context(FactVerification(False,scope_check=scope))
    assert revised=={'current_request':'戶外氧氣是否自備','unwanted_parts':['住宿介紹'],'event_errors':[],
                     'consultant_tasks':[],'consultant_policy_explanations':[]}
    contract_failure=FactVerification(True,scope_check={**scope,'revision_instructions':'无需修改'},
        contract_violations=['必须保留适用条件'])
    assert 'revision_instructions' not in scope_revision_context(contract_failure)


def test_exact_tail_removal_also_resolves_nested_audit_claims(monkeypatch):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    def unexpected(**kw):
        raise AssertionError('An exact unwanted tail requires no generative rewrite')
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',unexpected)
    tail='具體金額和適用門檻目前還沒有公布，這部分要請顧問幫您核對。'
    decision=EvaluationDecision('reply','peach_9d','other',reply='多人同行有優惠喔～'+tail,
        route_variant=ROUTE,evidence_refs=['route.shared.group_offer'])
    audit=FactVerification(False,unsupported_claims=['這部分要請顧問幫您核對'],
        scope_check={'unwanted_parts':[tail]},claim_checks=[{
            'claim':'多人同行有優惠','supported':True,'evidence':'route.shared.group_offer'}])
    revised,logs,_=revise_copy({'customer_text':'多人同行有優惠嗎？'},decision,audit,{'route.shared.group_offer'})
    assert revised['reply']=='多人同行有優惠喔。'
    assert revised['evidence_refs']==['route.shared.group_offer']
    assert logs[0]['node']=='v2_exact_sentence_revision'


def test_scope_style_preference_is_logged_but_substantive_defects_still_block():
    from app.reception_v2.reply_scope import parse_scope
    result=parse_scope({'current_request':'供氧條件','required_answer':'條件','contact_refusals':[],'profile_update_checks':[], 'traveler_age_checks':[],
        'requested_material_kinds':[],'material_request_checks':[],'consultant_task_checks':[],
        'unwanted_part_checks':[{'quote':'不能當作每天都有','kind':'style_only','reason':'wording preference'},
            {'quote':'再留LINE','kind':'unnecessary_question','reason':'not needed'}],
        'missing_answers':[],'event_errors':[],'event_error_checks':[],'question_checks':[]})
    assert result['unwanted_parts']==['再留LINE']
    assert len(result['unwanted_part_checks'])==2


def test_proactive_copy_pruning_removes_only_orphaned_images():
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    decision=EvaluationDecision('reply','peach_9d','other',
        reply='波密以外升級希爾頓。車內有供氧設備。',route_variant=ROUTE,
        evidence_refs=['route.shared.hotel_reference','route.shared.vehicle_reference'],
        material_keys=['routes12-hilton-oxygen','routes12-vehicle'])
    checked=FactVerification(True,scope_check={'unwanted_parts':['車內有供氧設備。']},
        claim_checks=[{'claim':'波密以外升級希爾頓','supported':True,'evidence':'route.shared.hotel_reference'},
                      {'claim':'車內有供氧設備','supported':True,'evidence':'route.shared.vehicle_reference'}])
    revised,_,_=revise_copy({'module':'silence_touch'},decision,checked,set(decision.evidence_refs))
    assert revised['material_keys']==['routes12-hilton-oxygen']
    assert revised['evidence_refs']==['route.shared.hotel_reference']


@pytest.mark.parametrize('reply,blocked',[
    ('9日與11日全程供氧，波密以外安排希爾頓。',True),
    ('十一日波密以外升級希爾頓。',True),
    ('9日全程供氧，波密以外安排希爾頓。',False),
    ('11日波密和珠峰段以外安排希爾頓。',False),
    ('11日住絨布旅館，其他地區除了波密安排希爾頓。',False),
])
def test_hotel_qualification_survives_semantic_audit_false_positive(monkeypatch,reply,blocked):
    from app.reception_v2 import runtime
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr(runtime,'call_reply_fact_verifier',lambda *a:(FactVerification(True),[],''))
    decision=EvaluationDecision('reply','peach_9d','other',reply=reply,route_variant=ROUTE,
        evidence_refs=['route.shared.hotel_reference'])
    checked,_,_=runtime._verify({'module':'reply'},decision)
    assert bool(checked.contract_violations)==blocked


def test_proactive_read_receipt_requires_replanning_new_value(monkeypatch):
    from app.reception_v2 import runtime
    from app.reception_v2.reply_revision import can_revise_copy
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr(runtime,'call_reply_fact_verifier',lambda *a:(FactVerification(True),[],''))
    decision=EvaluationDecision('reply','peach_9d','other',reply='行程圖收到了嗎？',route_variant=ROUTE)
    checked,_,_=runtime._verify({'module':'silence_touch',
        'v2_proactive_candidate_fact_ids':['route.shared.hotel_reference']},decision)
    assert not checked.relevant
    assert not can_revise_copy(decision,checked)


def test_repeated_topic_requires_new_plan_instead_of_same_topic_copy_repair():
    from app.reception_v2.reply_revision import can_revise_copy
    from app.reply_fact_verification import FactVerification
    decision=EvaluationDecision('reply','peach_9d','other',reply='重複住宿。')
    checked=FactVerification(True,scope_check={'unwanted_parts':['重複住宿。'],
        'unwanted_part_checks':[{'kind':'repeat_content','quote':'重複住宿。'}]},
        contract_violations=['重複住宿。'])
    assert not can_revise_copy(decision,checked)


def test_verified_profile_receipt_removes_audited_unasked_followup(monkeypatch):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('No copy model needed'))
    decision=EvaluationDecision('reply','peach_9d','other',reply='好的2位。想哪天出發？',
        slots={'party_size':'2'},slot_evidence={'party_size':'我們2位'},
        follow_up_question='想哪天出發？',v2_events=[{'type':'profile_updated','quote':'我們2位'}])
    checked=FactVerification(True,scope_check={'profile_ack_only':True,
        'profile_update_checks':[{'field':'party_size','persisted_correctly':True}],
        'unwanted_parts':['想哪天出發？']},contract_violations=['想哪天出發？'])
    revised,_,_=revise_copy({'customer_text':'我們2位'},decision,checked,set())
    assert revised['reply']=='好喔，同行2位。' and revised['follow_up_question']==''


def test_copy_revision_never_promotes_an_invented_citation(monkeypatch):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:
        (kw['parser']({'reply':'每位旅客有自己的大座位。','evidence_refs':['invented.vehicle']}),[],''))
    decision=EvaluationDecision('reply','peach_9d','other',reply='比較舒服。',
        evidence_refs=['route.shared.vehicle_reference'])
    revised,_,_=revise_copy({},decision,FactVerification(False,unsupported_claims=['比較舒服']),
        {'route.shared.vehicle_reference'})
    assert revised['evidence_refs']==['route.shared.vehicle_reference']
    assert 'invented.vehicle' not in revised['evidence_refs']


def test_repair_accepts_citation_established_by_independent_auditor(monkeypatch):
    import json
    from app.reception_v2 import runtime
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr(runtime.settings,'deepseek_api_key','test')
    original={'action':'reply','route_variant':ROUTE,'reply':'原稿','v2_events':[]}
    monkeypatch.setattr(runtime,'_call',lambda *a:({'content':json.dumps(original)},{'duration_ms':1}))
    calls=[]
    def verify(context,decision):
        calls.append(decision)
        return (FactVerification(False,unsupported_claims=['原稿']) if len(calls)==1 else FactVerification(True)),[],''
    monkeypatch.setattr(runtime,'_verify',verify)
    def revise(context,decision,audit,available):
        assert 'route.shared.hotel_reference' in available
        return {'reply':'住宿已修正。','evidence_refs':['route.shared.hotel_reference']},[],''
    monkeypatch.setattr('app.reception_v2.reply_revision.revise_copy',revise)
    decision,_,_,_=runtime.run_v2_agent({'module':'reply','customer_text':'住宿呢','route_variant':ROUTE})
    assert decision.evidence_refs==['route.shared.hotel_reference']


@pytest.mark.parametrize('text',['業務反饋主要是入藏函審批受限','业务反馈原因是审批受限','不能說免交健康證明'])
def test_internal_business_feedback_cannot_leak_into_customer_copy(text):
    from app.advisor_voice import v2_internal_copy_violation
    assert v2_internal_copy_violation(text)


@pytest.mark.parametrize('protected', ['none','human','contact','existing_handoff','rejected','missing_scope','task'])
def test_final_audit_removes_only_obsolete_knowledge_handoff(monkeypatch,protected):
    from app.reception_v2.runtime import _verify
    from app.reply_fact_verification import FactVerification
    decision=EvaluationDecision('handoff','peach_9d','other',reply='我們不拼內賓。',route_variant=ROUTE,
        handoff_reason='knowledge_confirmation_required',journey_stage='handoff')
    context={'module':'reply','journey':{'stage':'value_building'}}
    if protected=='human': decision.v2_events=[{'type':'human_requested'}]
    if protected=='contact': decision.contact_values={'wechat':'customer'}
    if protected=='existing_handoff': context['journey']['stage']='handoff'
    result=FactVerification(protected!='rejected',
        scope_check={} if protected=='missing_scope' else {'current_request':'拼團政策','consultant_tasks':[]},
        confirmation_questions=['實際未知條件'] if protected=='task' else [])
    monkeypatch.setattr('app.reception_v2.runtime.call_reply_fact_verifier',lambda *a:(result,[],''))
    checked,logs,_=_verify(context,decision)
    if protected=='none':
        assert decision.action=='reply' and decision.handoff_reason is None
        assert decision.journey_stage=='value_building' and checked.relevant
        assert logs[-1]['node']=='v2_handoff_reconciliation'
    else:
        assert decision.action=='handoff' and decision.handoff_reason=='knowledge_confirmation_required'


def test_missing_action_is_repaired_once_instead_of_silently_inferred(monkeypatch):
    import json
    import app.reception_v2.runtime as runtime
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    calls=[]
    def model(payload,round_index):
        calls.append(payload)
        value={'reply':'收到。','route_variant':ROUTE,'v2_events':[]}
        if len(calls)>1: value['action']='reply'
        return {'content':json.dumps(value)}, {'duration_ms':1,'status':'completed'}
    monkeypatch.setattr(runtime,'_call',model)
    monkeypatch.setattr(runtime,'_verify',lambda *a:(FactVerification(True),[],'verified'))
    result,_,_,_=runtime.run_v2_agent({'module':'reply','customer_text':'好','route_variant':ROUTE})
    assert result.action=='reply' and len(calls)==2


def test_exact_unwanted_sentence_is_removed_without_regenerating_state(monkeypatch):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    text='小費建議每人每天30元人民幣。這30元如何拆分我請顧問確認。'
    decision=EvaluationDecision('reply','peach_9d','other',reply=text,route_variant=ROUTE,
        evidence_refs=['route.shared.tips'])
    audit=FactVerification(True,claim_checks=[{'claim':'小費建議每人每天30元人民幣','supported':True,
        'evidence':'route.shared.tips'}],scope_check={'unwanted_parts':['這30元如何拆分我請顧問確認']})
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('unneeded regeneration'))
    result,_,_=revise_copy({},decision,audit,{'route.shared.tips'})
    assert result['reply']=='小費建議每人每天30元人民幣。'
    assert result['evidence_refs']==['route.shared.tips'] and decision.reply==text


def test_exact_unwanted_trailing_clause_preserves_direct_answer(monkeypatch):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    decision=EvaluationDecision('reply','peach_9d','other',
        reply='這條行程不安排納木錯，也不去珠峰。',route_variant=ROUTE,
        evidence_refs=['route.9.summary'])
    audit=FactVerification(True,claim_checks=[{'claim':'這條行程不安排納木錯','supported':True,
        'evidence':'route.9.summary'}],scope_check={'unwanted_parts':['也不去珠峰']})
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('unneeded regeneration'))
    result,_,_=revise_copy({},decision,audit,{'route.9.summary'})
    assert result['reply']=='這條行程不安排納木錯。'
    assert result['evidence_refs']==['route.9.summary']


def test_unsupported_sentence_is_deleted_with_its_conditions(monkeypatch):
    from app.reception_v2.reply_revision import revise_copy
    from app.reply_fact_verification import FactVerification
    decision=EvaluationDecision('reply','peach_9d','other',
        reply='小費建議每人每天30元人民幣。如果您想要，顧問保證可以額外安排。',
        route_variant=ROUTE,evidence_refs=['route.shared.tips'])
    audit=FactVerification(False,unsupported_claims=['顧問保證可以額外安排'],
        claim_checks=[{'claim':'小費建議每人每天30元人民幣','supported':True,'evidence':'route.shared.tips'}])
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('unneeded regeneration'))
    result,_,_=revise_copy({},decision,audit,{'route.shared.tips'})
    assert result['reply']=='小費建議每人每天30元人民幣。'


def test_calendar_evidence_computes_validated_dates_without_guessing_years():
    from app.reception_v2.fact_proof import calendar_facts
    assert calendar_facts({'departure_window':'2027-03-28'})==[{'date':'2027-03-28','weekday':'星期日'}]
    assert calendar_facts({'departure_window':'3月28日'})==[]
    assert calendar_facts({'departure_window':'3月28日'},'2027 林芝桃花 9 日')==[{'date':'2027-03-28','weekday':'星期日'}]
    assert calendar_facts({'departure_window':'2027-02-30'})==[]


def test_appointment_without_contact_identifier_is_not_captured():
    import json
    from app.reception_v2.runtime import _validated_decision
    decision = _validated_decision({'content':json.dumps({'action':'handoff','route_variant':ROUTE,
        'reply':'好的，明天再聯繫。','lead_action':'captured','handoff_reason':'lead_captured',
        'v2_events':[{'type':'contact_agreed','quote':'明天十点联系',
                      'contact_at':'2026-09-21T10:00:00+08:00'}]})},set(),set(),
        {'module':'reply','customer_text':'明天十点联系','now':NOW})
    assert decision.lead_action=='none' and decision.action=='reply'
    assert decision.v2_events[0]['type']=='contact_agreed'


def test_missing_event_field_is_not_silently_treated_as_no_customer_request():
    import json
    from app.reception_v2.runtime import _validated_decision
    with pytest.raises(ValueError,match='v2_invalid_events'):
        _validated_decision({'content':json.dumps({'action':'reply','reply':'您好'})},set(),set(),{})


def test_independent_scope_material_intent_detects_main_agent_omission(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    def model(**kw):
        return kw['parser']({'current_request':'索取高反附件','required_answer':'提供附件','contact_refusals':[],'profile_update_checks':[], 'traveler_age_checks':[],
            'requested_material_kinds':['altitude'],'material_request_checks':[{'kind':'altitude','quote':'給我附件'}],'consultant_task_checks':[],
            'unwanted_part_checks':[],'missing_answers':[],'event_errors':[],'event_error_checks':[],'question_checks':[]}),[],'digest'
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    result,_,_=verify_scope({'customer_message':'給我附件','v2_events':[{'type':'question','quote':'給我附件'}]})
    assert len(result['event_errors'])==1
    result,_,_=verify_scope({'customer_message':'給我附件','v2_events':[{'type':'material_requested','material_kind':'altitude'}]})
    assert not result['event_errors']


def test_repaired_body_cannot_retain_rejected_original_text(monkeypatch):
    import json
    from app.reception_v2 import runtime
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr(runtime.settings,'deepseek_api_key','test')
    original={'action':'reply','route_variant':ROUTE,'reply':'舊正文','reply_body':'舊正文','v2_events':[]}
    monkeypatch.setattr(runtime,'_call',lambda *a:({'content':json.dumps(original)},{'duration_ms':1}))
    observed=[]
    def verify(context,decision):
        observed.append(decision.reply_body or decision.reply)
        return (FactVerification(False,unsupported_claims=['舊正文']) if len(observed)==1
                else FactVerification(True)),[],'checked'
    monkeypatch.setattr(runtime,'_verify',verify)
    monkeypatch.setattr('app.reception_v2.reply_revision.revise_copy',lambda *a:({'reply':'已修正正文','evidence_refs':[]},[],''))
    decision,_,_,_=runtime.run_v2_agent({'module':'reply','customer_text':'好','route_variant':ROUTE})
    assert observed==['舊正文','已修正正文']
    assert (decision.reply_body or decision.reply)=='已修正正文'


def test_second_copy_repair_preserves_latest_validated_decision(monkeypatch):
    import json
    from app.reception_v2 import runtime
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr(runtime.settings,'deepseek_api_key','test')
    original={'action':'reply','route_variant':ROUTE,'reply':'原稿','v2_events':[]}
    monkeypatch.setattr(runtime,'_call',lambda *a:({'content':json.dumps(original)},{'duration_ms':1}))
    observed=[]
    def verify(context,decision):
        observed.append(decision.reply)
        return (FactVerification(False,unsupported_claims=[decision.reply]) if len(observed)<3
                else FactVerification(True)),[],'checked'
    monkeypatch.setattr(runtime,'_verify',verify)
    revised=[]
    def revise(context,decision,*args):
        revised.append(decision.reply)
        return {'reply':f'修正{len(revised)}','evidence_refs':[]},[],''
    monkeypatch.setattr('app.reception_v2.reply_revision.revise_copy',revise)
    decision,_,_,_=runtime.run_v2_agent({'module':'reply','customer_text':'好','route_variant':ROUTE})
    assert revised==['原稿','修正1'] and observed==['原稿','修正1','修正2']
    assert decision.action=='reply' and decision.reply=='修正2'


def test_copy_repair_is_bounded_and_never_returns_an_unverified_reply(monkeypatch):
    import json
    from app.reception_v2 import runtime
    from app.reply_fact_verification import FactVerification
    from app.deepseek_evaluation import EvaluationCallError
    monkeypatch.setattr(runtime.settings,'deepseek_api_key','test')
    original={'action':'reply','route_variant':ROUTE,'reply':'原稿','v2_events':[]}
    monkeypatch.setattr(runtime,'_call',lambda *a:({'content':json.dumps(original)},{'duration_ms':1}))
    monkeypatch.setattr(runtime,'_verify',lambda *a:(FactVerification(False,unsupported_claims=['失败']),[],''))
    calls=[]
    def revise(*a):
        calls.append(1)
        return {'reply':'仍不支持','evidence_refs':[]},[],''
    monkeypatch.setattr('app.reception_v2.reply_revision.revise_copy',revise)
    with pytest.raises(EvaluationCallError,match='v2_reply_verification_failed'):
        runtime.run_v2_agent({'module':'reply','customer_text':'好','route_variant':ROUTE})
    assert len(calls)==2


def test_plain_itinerary_request_compiles_caption_before_model_length_check():
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','route_variant':ROUTE,'reply':'冗長模型摘要'*100,
         'v2_events':[{'type':'material_requested','quote':'給我行程圖','material_kind':'itinerary'}]}
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':'reply','customer_text':'給我行程圖'})
    _enforce_delivery_contract({'available_materials':[{'key':'routes12-9d-itinerary'}]},decision)
    assert decision.reply==ROUTES[ROUTE]['groups']['itinerary_overview']['text']
    assert decision.material_keys==['routes12-9d-itinerary']


def test_scope_receives_actual_planned_attachments(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    seen=[]
    def model(**kw):
        seen.append(kw['input_data'])
        return {'requested_material_kinds':[],'event_errors':[],'contact_refusals':[]},[],''
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    verify_scope({'selected_route':ROUTE,'selected_asset_ids':['route-itinerary'], 'allowed_asset_claims':[{'id':'route-itinerary','text':'行程圖'}]})
    assert seen[0]['selected_route']==ROUTE
    assert seen[0]['selected_asset_ids']==['route-itinerary']
    assert seen[0]['attachment_reference_metadata'][0]['text']=='行程圖'
    assert 'allowed_asset_claims' not in seen[0]


@pytest.mark.parametrize('events,missing',[
    ([],True),
    ([{'type':'contact_refused','scope':'LINE'}],True),
    ([{'type':'contact_refused','scope':'all'}],False),
])
def test_scope_requires_persistable_refusal_not_just_polite_acknowledgment(monkeypatch,events,missing):
    from app.reception_v2.reply_scope import verify_scope
    def model(**kw):
        return {'requested_material_kinds':[],'event_errors':[],
            'contact_refusals':[{'quote':'不要再推銷','scope':'all'}]},[],''
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    audit,_,_=verify_scope({'customer_message':'請不要再推銷','event':'customer_message',
        'proposed_body':'好的，不打擾您。','v2_events':events})
    assert bool(audit['event_errors'])==missing


def test_multipart_extra_answer_cannot_hide_a_question_in_the_middle(monkeypatch):
    from app.reception_v2.runtime import _verify
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr('app.reception_v2.runtime.call_reply_fact_verifier',lambda *a:(FactVerification(True),[],''))
    decision=EvaluationDecision('reply','peach_9d','other',reply='林芝接機。',route_variant=ROUTE,
        v2_delivery_sections=[{'group_key':'itinerary_overview','text':'林芝接機。您何時出發？三月有出發。',
                              'asset_keys':[],'evidence_refs':[]}])
    result,_,_=_verify({'module':'reply'},decision)
    assert not result.relevant
    assert any('正文不得夹带追问' in item for item in result.contract_violations)


def test_semantic_audit_cannot_hide_rejected_claim_behind_global_true():
    from app.reply_fact_verification import _parse_v2
    result = _parse_v2({'supported':True,'relevant':True,'unsupported_claims':[],
        'claim_checks':[{'claim':'供氧保證安全','supported':False,'evidence':'沒有此保證'}],
        'scope_check':{'current_request':'有氧就安全嗎','unrelated_claims':['順便留LINE'],
                       'missing_answers':[],'consultant_tasks':[]}})
    assert not result.supported and not result.relevant
    assert result.unsupported_claims == ['供氧保證安全']
    assert result.contract_violations == ['順便留LINE']


@pytest.mark.parametrize('case', ['disabled_route','switch_disabled','lead_disabled','image_limit'])
def test_v2_published_policy_changes_are_enforced_even_if_semantic_verifier_passes(monkeypatch,case):
    import app.reception_v2.runtime as runtime
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr(runtime,'call_reply_fact_verifier',lambda *a:(FactVerification(True),[],''))
    decision=EvaluationDecision('reply','peach_9d','other',reply='為您說明。',route_variant=ROUTE)
    context={'module':'reply','route_variant':ROUTE,'reception_policy':{}}
    if case=='disabled_route':context['reception_policy']={'route_switch':{'allowed_routes':['peach_11d_2027']}}
    if case=='switch_disabled':
        decision.route_variant='peach_11d_2027'
        context['reception_policy']={'route_switch':{'enabled':False}}
    if case=='lead_disabled':
        decision.lead_action='ask'
        context['reception_policy']={'operator_configuration':{'lead_capture':{'enabled':False}}}
    if case=='image_limit':
        decision.material_keys=['routes12-hilton-room','routes12-hilton-oxygen']
        context['reception_policy']={'reply_style':{'max_images_per_turn':1}}
    result,_,_=runtime._verify(context,decision)
    assert not result.relevant and result.contract_violations


def test_new_question_clears_considering_but_preserves_explicit_time():
    slots = merge_events({}, event('considering', '先考虑一下'))
    slots = merge_events(slots, event('contact_agreed', '下午联系', contact_at='2026-09-20T08:00:00+00:00'))
    slots = merge_events(slots, event('question', '在哪里集合'))
    state = slots['_v2_state']
    assert 'reevaluate_at' not in state and 'waiting_reason' not in state
    assert contact_constraint({'memory': slots, 'now': NOW}) == ('customer_requested_time', 360)


def test_new_question_does_not_revoke_global_optout_but_explicit_agreement_does():
    slots = merge_events({}, event('contact_refused', '不要联系', scope='all'))
    slots = merge_events(slots, event('question', '在哪里集合'))
    assert contact_constraint({'memory': slots, 'now': NOW}) == ('customer_opted_out', 0)
    slots = merge_events(slots, event('contact_agreed', '还是下午联系我', contact_at='2026-09-20T08:00:00+00:00'))
    assert contact_constraint({'memory': slots, 'now': NOW}) == ('customer_requested_time', 360)


@pytest.mark.parametrize('target', ['v1','v2'])
def test_engine_projection_drops_inferred_terminal_stage_but_keeps_customer_evidence(session_factory, target):
    from test_reception_engine_switch import _seed_state
    from app.reception_v2.engine_projection import project_engine_state
    sid = _seed_state(session_factory)
    with session_factory() as db:
        state = db.get(ConversationState, sid)
        slots = merge_events({'party_size': 6}, event('contact_refused', '不留LINE', scope='LINE'))
        journey = ConversationJourney(conversation_state_id=sid, route_variant=ROUTE, stage='captured', slots=slots)
        db.add(journey)
        db.flush()
        project_engine_state(db, state, 'v2' if target == 'v1' else 'v1', target, NOW)
        db.commit()
        db.expire_all()
        assert journey.stage == 'value_building'
        assert journey.slots['party_size'] == 6
        assert journey.slots['_v2_state']['refused_channels'] == ['LINE']


def test_projection_preserves_real_handoff(session_factory):
    from test_reception_engine_switch import _seed_state
    from app.reception_v2.engine_projection import project_engine_state
    sid = _seed_state(session_factory)
    with session_factory() as db:
        state = db.get(ConversationState, sid)
        journey = ConversationJourney(conversation_state_id=sid, route_variant=ROUTE, stage='value_building')
        db.add_all([journey, HandoffTask(conversation_state_id=sid, reason_code='requested_material_unavailable', status='pending')])
        db.flush()
        project_engine_state(db, state, 'v2', 'v1', NOW)
        assert journey.stage == 'handoff'


@pytest.mark.parametrize('kind', ['itinerary','full_introduction','hotel','vehicle','altitude'])
def test_missing_requested_material_is_actionable_handoff_without_false_receipt(kind):
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='other', route_variant=ROUTE,
        reply='資料給您', v2_events=event('material_requested', '给我资料', material_kind=kind))
    _enforce_delivery_contract({'available_materials': []}, decision)
    assert decision.action == 'handoff' and decision.handoff_reason == 'requested_material_unavailable'
    assert not decision.material_keys and not decision.v2_delivery_sections
    assert answer_receipt(asdict(decision), decision.reply) is None


def test_future_contact_outside_channel_window_is_handoff_with_original_date():
    when = '2026-09-22T08:00:00+08:00'
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='other', route_variant=ROUTE,
        reply='好的', v2_events=event('contact_agreed', '后天早上八点联系', contact_at=when))
    _enforce_delivery_contract({'now': NOW, 'last_customer_at': NOW}, decision)
    assert decision.action == 'handoff' and decision.handoff_reason == 'customer_contact_outside_window'
    assert decision.v2_events[0]['contact_at'] == when


def test_full_introduction_keeps_extra_answer_and_receipts_are_independent():
    questions = event('question', '小费多少')
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='other', route_variant=ROUTE,
        reply='小費每人每天30元。', evidence_refs=['service.tips'], slots={'party_size': 6},
        v2_events=[*questions, *event('material_requested', '给完整介绍', material_kind='full_introduction')])
    materials = [{'key': key} for group in ROUTES[ROUTE]['groups'].values() for key in group['assets']]
    _enforce_delivery_contract({'available_materials': materials}, decision)
    sections = decision.v2_delivery_sections
    assert sections[0]['answers_customer_question']
    assert all(s['group_key'] != 'party_question' for s in sections)
    assert answer_receipt(asdict(decision), sections[0]['text'])['questions'] == questions
    assert answer_receipt(asdict(decision), sections[1]['text'])['questions'] == []


def test_material_retrieval_does_not_hide_assets_when_keywords_do_not_match():
    from app.reception_v2.tools import execute_tool
    from app.reception_v2.skill_registry import SkillRegistry
    result = execute_tool('get_route_materials', {'route_variant': ROUTE, 'topic': '让我看看长什么样'}, SkillRegistry())
    assert {m['key'] for m in result['materials']} == {
        key for group in ROUTES[ROUTE]['groups'].values() for key in group['assets']}


def test_third_route_registers_followup_candidates_without_editing_runtime(tmp_path, monkeypatch):
    from app.reception_v2 import route_profiles, skill_registry, flow_classifier
    root = tmp_path / 'skills'
    root.mkdir()
    (root / 'SKILL.md').write_text('---\nname: island-5d\ndescription: 海岛五日\nroute_variant: island_5d\nfollowup_groups: island\nroute_aliases: 海岛五日|5日海岛\n---\n只使用海岛批准事实。', encoding='utf-8')
    registry = skill_registry.SkillRegistry(root)
    spec = deepcopy(ROUTES[ROUTE])
    spec.update(name='海岛五日', branch='island_5d', groups={'island': {
        'text': '珊瑚岛风光', 'purpose': '自然景观', 'assets': [], 'evidence': ['island.nature']}})
    monkeypatch.setitem(ROUTES, 'island_5d', spec)
    monkeypatch.setattr(skill_registry, 'SkillRegistry', lambda: registry)
    monkeypatch.setattr(flow_classifier, 'SkillRegistry', lambda: registry)
    profile = route_profiles.route_profile('island_5d')
    assert profile['skill'] == 'island-5d'
    assert profile['followup_candidates'] == ('island.nature',)
    assert flow_classifier.infer_route_variant('我想参加海岛五日') == 'island_5d'
    import json
    from app.reception_v2.runtime import _validated_decision
    decision = _validated_decision({'content':json.dumps({'action':'reply','route_variant':'island_5d','v2_events':[],
        'reply':'這條行程可以欣賞珊瑚島風光。','evidence_refs':['island.nature']})},
        {'island.nature'},set(),{'module':'reply','customer_text':'介紹一下海島五日'})
    assert decision.branch == 'island_5d' and decision.route_variant == 'island_5d'


def test_published_opening_is_frozen_as_delivery_items():
    decision = EvaluationDecision(action='reply', branch='unclassified', intent='route_intro', reply='你好')
    decision.delivery_intent = 'opening'
    _enforce_delivery_contract({'context_messages': [], 'reception_policy': {
        'operator_configuration': {'opening_messages': ['歡迎來諮詢。', '您想了解哪條行程？'], 'opening_interval_seconds': 2}}}, decision)
    assert decision.opening_messages == ['歡迎來諮詢。', '您想了解哪條行程？']
    assert len(decision.reply_options) == len(ROUTES)


def test_deferred_sandbox_job_creates_new_execution_at_next_due_time(session_factory, monkeypatch):
    from test_playground_journey import setup_journey, bind_new_route_snapshot
    from app.automation_models import RehearsalEnrollment, RehearsalJob, AutomationRun
    from app.automation_service import enroll_rehearsal, advance_sops, process_automation_run
    from app.reception_v2 import ENGINE_RELEASE_ID
    with session_factory() as db:
        session, version = setup_journey(db)
        bind_new_route_snapshot(session)
        session.engine_version, session.engine_release_id = 'v2', ENGINE_RELEASE_ID
        session.due_at = None
        for old in db.scalars(select(RehearsalEnrollment)):
            old.status = 'completed'
        db.flush()
        enrollment = enroll_rehearsal(db, session, version, source='model_route', reenroll=True,
            request_key='defer-integration', schedule_intervals=[1])
        job = db.scalar(select(RehearsalJob).where(RehearsalJob.enrollment_id == enrollment.id))
        session.virtual_now = job.scheduled_at
        advance_sops(db, session); db.commit()
        first = db.scalar(select(AutomationRun).where(AutomationRun.module == 'silence_touch'))
        assert first
        decision = EvaluationDecision(action='no_action', branch='peach_9d', intent='other',
            route_variant=ROUTE, journey_stage='value_building', wakeup_action='defer', defer_minutes=5)
        monkeypatch.setattr('app.automation_service.generate_decision', lambda _: (decision, [], '', {}))
        assert process_automation_run(db, environment='playground')
        db.refresh(job)
        assert job.status == 'scheduled'
        session.virtual_now = job.scheduled_at
        advance_sops(db, session); db.commit()
        runs = db.scalars(select(AutomationRun).where(AutomationRun.module == 'silence_touch')).all()
        assert len(runs) == 2
        assert len({r.idempotency_key for r in runs}) == 2


@pytest.mark.parametrize('engine', ['v1','v2'])
def test_sandbox_static_proactive_gate_obeys_persisted_customer_optout(session_factory, engine):
    from test_playground_journey import setup_journey
    from app.automation_service import gate
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.engine_version = engine
        session.due_at = None
        session.controls = {**session.controls, 'journey': {'slots': {'_v2_state': {'proactive_opt_out': True}}}}
        assert gate(session, session.virtual_now, True, db) == 'customer_opted_out'
        assert gate(session, session.virtual_now, False, db) is None


@pytest.mark.parametrize('available', [False, True])
def test_captured_contact_attaches_only_available_guide_or_records_specific_pending_work(available):
    decision = EvaluationDecision(action='handoff', branch='peach_9d', intent='contact', route_variant=ROUTE,
        reply='收到', lead_action='captured', contact_values={'wechat':'test_travel'}, handoff_reason='lead_captured')
    _enforce_delivery_contract({'available_materials': [{'key':'china2go-altitude-guide-v1'}] if available else []}, decision)
    assert decision.action == 'handoff' and decision.lead_action == 'captured'
    if available:
        assert decision.material_keys == ['china2go-altitude-guide-v1']
        assert decision.covered_content_groups == ['altitude_guide']
    else:
        assert not decision.material_keys
        assert 'pending_material:altitude_guide' in decision.safety_flags
        assert '補給' in decision.reply


def test_pdf_replacement_requires_new_approval_and_cannot_inherit_image_approval(session_factory, tmp_path):
    import hashlib
    from app.material_library import replace_asset_binding, candidate_materials
    from app.models import KnowledgeVersion, MaterialAsset, StoredMedia
    from app.route_packages import ROUTE_PACKAGES
    with session_factory() as db:
        version = KnowledgeVersion(tenant_id=1,version_key=ROUTE_PACKAGES[ROUTE]['knowledge_version'],title='test',content_hash='x')
        db.add(version); db.flush()
        path=tmp_path/'approved.pdf'; path.write_bytes(b'%PDF-1.4\nfixture old')
        media=StoredMedia(tenant_id=1,created_by=1,original_name=path.name,media_type='file',mime_type='application/pdf',file_size=path.stat().st_size,storage_path=str(path))
        db.add(media); db.flush()
        asset=MaterialAsset(knowledge_version_id=version.id,asset_key='china2go-altitude-guide-v1',source_path=str(path),
            display_name='guide',media_type='file',available=True,file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            metadata_json={'stored_media_id':media.id,'review_state':'evaluation_ready','live_approved':True,'route_variants':[ROUTE]})
        db.add(asset); db.commit()
        newpath=tmp_path/'replacement.pdf'; newpath.write_bytes(b'%PDF-1.4\nchanged content')
        replacement=StoredMedia(tenant_id=1,created_by=1,original_name=newpath.name,media_type='file',mime_type='application/pdf',file_size=newpath.stat().st_size,storage_path=str(newpath))
        db.add(replacement); db.flush()
        replace_asset_binding(db,asset,replacement)
        db.commit()
        assert asset.metadata_json['live_approved'] is False
        assert asset.metadata_json['review_state'] == 'pending'
        assert candidate_materials(db,1) == []
