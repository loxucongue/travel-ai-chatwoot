import json
import pytest
from app.reception_v2 import runtime
from app.deepseek_evaluation import EvaluationCallError
from app.reply_fact_verification import FactVerification

@pytest.fixture(autouse=True)
def configured_opening(monkeypatch):
    from app.reception_config import default_reception_configuration,policy_from_configuration
    from app.reception_policy_views import views_for_context
    policy=policy_from_configuration(default_reception_configuration())
    monkeypatch.setattr(runtime,'views_for_context',lambda context:views_for_context({'reception_policy':policy,**context}))

@pytest.mark.parametrize('reply_field',['none','reply_body'])
def test_server_opening_is_compiled_before_required_body_parser(reply_field):
    raw={'action':'reply','delivery_intent':'opening','v2_events':[]}
    if reply_field=='reply_body':raw['reply_body']='您好'
    result=runtime._validated_decision({'content':json.dumps(raw)},set(),set(),{'module':'reply','customer_text':'您好'})
    assert result.reply and 'China2Go' in result.reply
    runtime._enforce_delivery_contract({'module':'reply'},result)
    assert result.reply==result.reply_body and result.route_variant==''


def test_reply_body_alias_is_the_same_text_not_a_generated_fallback():
    raw={'action':'reply','reply_body':'在林芝集合。','v2_events':[{'type':'question','quote':'在哪集合？'}]}
    result=runtime._validated_decision({'content':json.dumps(raw)},set(),set(),{'module':'reply','customer_text':'在哪集合？'})
    assert result.reply=='在林芝集合。'

@pytest.mark.parametrize('context,events',[
    ({'route_variant':'peach_9d_2027'},[]),
    ({'context_messages':[{'role':'assistant','content':'之前的回答'}]},[]),
    ({},[{'type':'question','quote':'在哪集合？'}]),
])
def test_empty_opening_cannot_bypass_bound_route_history_or_question(context,events):
    raw={'action':'reply','delivery_intent':'opening','v2_events':events}
    with pytest.raises(ValueError,match='reply_missing'):
        runtime._validated_decision({'content':json.dumps(raw)},set(),set(),{'module':'reply','customer_text':'在哪集合？',**context})


def test_invented_opening_for_a_real_question_still_requires_independent_audit(monkeypatch):
    monkeypatch.setattr(runtime.settings,'deepseek_api_key','test')
    raw={'action':'reply','delivery_intent':'opening','v2_events':[]}
    monkeypatch.setattr(runtime,'_call',lambda *args:({'content':json.dumps(raw)},{'duration_ms':1}))
    checked=[]
    def verify(context,decision):
        checked.append(decision.reply)
        return FactVerification(False,unanswered_questions=['集合问题未回答']),[],''
    monkeypatch.setattr(runtime,'_verify',verify)
    with pytest.raises(EvaluationCallError):runtime.run_v2_agent({'module':'reply','customer_text':'在哪集合？'})
    assert checked


@pytest.mark.parametrize('body,expected',[
 ('65至75歲臺灣旅客需要健康證明，您這個年齡不用。',True),
 ('65至75岁台湾旅客需要健康证明，您这个年龄不用。',True),
 ('65至75歲臺灣旅客需要健康證明，這個年紀不需要提交。',True),
 ('65至75歲臺灣旅客需要健康證明，您這個年齡不用擔心其他年齡的限制。',False),
 ('健康證明不能說您這個年齡不用。',False),
 ('年齡不是最低門檻，64歲可以報名。',False),
])
def test_age_anaphora_does_not_invent_a_certificate_exemption(body,expected):
    from app.reception_v2.claim_guards import unsupported_certificate_waiver
    assert unsupported_certificate_waiver(body) is expected


def test_certificate_waiver_is_blocked_even_if_model_auditor_approves(monkeypatch):
    from app.deepseek_evaluation import EvaluationDecision
    monkeypatch.setattr('app.reception_v2.runtime.call_reply_fact_verifier',lambda *args:(FactVerification(True),[],''))
    decision=EvaluationDecision('reply','peach_9d','other',route_variant='peach_9d_2027',
        reply='65至75歲臺灣旅客需要健康證明，您這個年齡不用。',evidence_refs=['service.peach_age'])
    audit,*_=runtime._verify({'module':'reply','customer_text':'64歲能報名嗎？'},decision)
    assert not audit.relevant and any('豁免' in item for item in audit.contract_violations)


def test_missing_condition_repairs_copy_instead_of_discarding_known_answer(monkeypatch):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reception_v2 import reply_revision
    decision=EvaluationDecision('handoff','peach_9d','other',route_variant='peach_9d_2027',
        reply='每人提供氧氣瓶。各區段供應請顧問核對。',evidence_refs=['service.safety'],
        handoff_reason='knowledge_confirmation_required')
    audit=FactVerification(False,unsupported_claims=['每人提供氧氣瓶。'],
        contract_violations=['引用事实service.safety必须明确保留适用条件：海拔条件'],
        scope_check={'consultant_tasks':['核對區段供應']})
    assert reply_revision.can_revise_copy(decision,audit)
    calls=[]
    def node(**kwargs):
        calls.append(kwargs)
        return {'reply':'合格修復','evidence_refs':['service.safety']},[],''
    monkeypatch.setattr(reply_revision,'call_json_node',node)
    assert reply_revision.prune_copy({},decision,audit,{'service.safety'}) is None
    result=reply_revision.revise_copy({},decision,audit,{'service.safety'})
    assert result[0]['reply']=='合格修復' and len(calls)==1
    assert calls[0]['input_data']['violations']==audit.contract_violations
    assert 'service.safety' in calls[0]['input_data']['approved_facts']


@pytest.mark.parametrize('still_missing',[True,False])
def test_conflicting_coverage_requires_grounded_recheck_not_automatic_approval(monkeypatch,still_missing):
    from app.reception_v2.reply_scope import verify_scope
    base={'current_request':'在哪集合？','required_answer':'集合地点','event_error_checks':[],
        'question_checks':[{'request_quote':'在哪集合？','request_kind':'fact','answer_kind':'text',
            'answer_segment_ids':['reply_0'],'answer_asset_ids':[],'covered':True}],
        'material_request_checks':[],'profile_update_checks':[],'traveler_age_checks':[],
        'contact_refusals':[],'requested_material_kinds':[],'consultant_task_checks':[],
        'unwanted_part_checks':[],'missing_answers':['集合说明']}
    calls=[]
    def model(**kw):
        calls.append(kw)
        value=dict(base)
        if len(calls)==2:value['missing_answers']=['集合说明'] if still_missing else []
        return kw['parser'](value),[{'node':kw['node']}],str(len(calls))
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    result,logs,_=verify_scope({'customer_message':'在哪集合？','proposed_body':'在林芝接機。'})
    assert bool(result['missing_answers']) is still_missing
    assert len(calls)==2 and len(logs)==2
    assert calls[1]['node']=='v2_scope_coverage_recheck'
    assert calls[1].get('reasoning_effort') is None
    assert calls[1]['max_tokens']==1800
    assert calls[1]['input_data']['actual_answer_segments']==calls[0]['input_data']['actual_answer_segments']
