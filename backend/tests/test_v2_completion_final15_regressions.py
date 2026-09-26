"""Observed acceptance failures, including auditors that incorrectly pass text."""
import pytest

from app.reception_v2.reply_scope import verify_scope


@pytest.mark.parametrize('body,blocked',[
    ('紅景天、丹木斯這類預防高反的藥，劑量要問醫師。',True),
    ('紅景天不是預防高反的藥。',False),
    ('不能把紅景天說成預防高反的藥。',False),
    ('紅景天、丹木斯這類產品，請醫師評估。',False),
    ('請醫師評估紅景天是否適合您。',False),
])
def test_medical_label_guard_preserves_negation(body,blocked):
    from app.reception_v2.claim_guards import unsupported_prevention_label
    assert unsupported_prevention_label(body)==blocked


def mock_scope(monkeypatch, **extra):
    result={'event_errors':[], 'event_error_checks':[], 'question_checks':[],
            'contact_refusals':[], 'material_request_checks':[], **extra}
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(result,[],''))


def test_historical_freeform_error_cannot_trigger_profile_repair(monkeypatch):
    mock_scope(monkeypatch,event_errors=['本轮改成6位未保存'],
               event_error_checks=[{'quote':'改成6位一起','reason':'未保存人数'}])
    result=verify_scope({'customer_message':'那6人價格多少？',
        'recent_conversation':[{'role':'customer','content':'改成6位一起'}]})[0]
    assert result['event_errors']==[]


def test_current_event_error_keeps_grounded_evidence(monkeypatch):
    mock_scope(monkeypatch,event_error_checks=[{'quote':'不要聯絡','reason':'遗漏拒绝事件'}])
    assert verify_scope({'customer_message':'不要聯絡'})[0]['event_errors']==['遗漏拒绝事件']


def test_deleted_task_quote_cannot_reject_the_repaired_reply(monkeypatch):
    deleted='具體金額要請顧問核對。'
    mock_scope(monkeypatch,consultant_task_checks=[{'task':'核對優惠金額','needed':False,'draft_quote':deleted}],
        unwanted_parts=[deleted],unwanted_part_checks=[{'quote':deleted,'kind':'unrequested_task'}])
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',lambda *a:pytest.fail('No actual task remains'))
    result=verify_scope({'customer_message':'多人有優惠嗎？','proposed_body':'有多人同行優惠。'})[0]
    assert not result.get('task_scope_errors') and result['unwanted_parts']==[]


def test_invented_event_error_quote_rejected(monkeypatch):
    mock_scope(monkeypatch,event_error_checks=[{'quote':'明天聯絡','reason':'遗漏预约'}])
    with pytest.raises(ValueError,match='event_evidence_invalid'):
        verify_scope({'customer_message':'價格多少？'})


def test_reference_fact_is_not_a_delivered_answer(monkeypatch):
    mock_scope(monkeypatch,question_checks=[{'request_quote':'小費多少','answer_quote':'每天30元','covered':True}])
    result=verify_scope({'customer_message':'給我完整介紹，也回答小費多少',
        'proposed_body':'這是完整9日介紹，第一天林芝接機。','allowed_facts':[{'text':'每天30元'}],
        'ordered_delivery_sections':[{'text':'這是完整介紹'}],
        'v2_events':[{'type':'material_requested'}]})[0]
    assert result['missing_answers'] and result['event_errors']


@pytest.mark.parametrize('needed,remove',[(True,False),(False,False),(False,True)])
def test_task_proof_three_states_have_distinct_effects(monkeypatch,needed,remove):
    quote='顧問會依餐廳條件協助確認。'
    mock_scope(monkeypatch,consultant_task_checks=[{'task':'餐廳條件確認','draft_quote':quote,'needed':False}],
        unwanted_parts=[quote],unwanted_part_checks=[{'quote':quote,'kind':'unrequested_task'}])
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',lambda *a:(
        [{'index':0,'needed':needed,'remove_from_reply':remove,'reason':'independent proof'}],[],''))
    result=verify_scope({'customer_message':'素食能配合嗎？','proposed_body':quote})[0]
    assert bool(result['consultant_tasks'])==needed
    assert bool(result['consultant_policy_explanations'])==(not needed and not remove)
    assert bool(result['task_scope_errors'])==remove
    if not needed and not remove:
        assert result['unwanted_parts']==[]


def test_display_conversion_keeps_urls_literal_and_is_idempotent():
    from app.advisor_voice import normalize_v2_customer_copy
    text='绒布旅馆房间有独立卫生间和供氧设备。https://example.com/房间?q=设备'
    actual=normalize_v2_customer_copy(text)
    assert '絨布' in actual and '設備' in actual and '房間' in actual
    assert actual.endswith('https://example.com/房间?q=设备')
    assert normalize_v2_customer_copy(actual)==actual


@pytest.mark.parametrize('text,bad',[
    ('目前批准的配置是2025年VIP車',True),('資料上沒有明確公布',True),
    ('我不能直接說要或不用',True),('公司大團客製門檻',True),
    ('入藏函是否批准以實際審批結果為準',False),('供氧不能保證不會高反',False),
    ('台灣65至75歲旅客需健康證明',False)])
def test_internal_wording_and_legitimate_conditions_are_distinct(text,bad):
    from app.advisor_voice import v2_internal_copy_violation
    assert bool(v2_internal_copy_violation(text))==bad


def test_rejected_multi_slot_followup_clears_structural_fields(monkeypatch):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import revise_copy
    tail='您大概幾位同行、想抓哪個時間呢？'
    decision=EvaluationDecision('reply','peach_9d','other',reply='第一天林芝接機。'+tail,
        follow_up_question=tail,follow_up_type='clarification',follow_up_field='topic',
        evidence_refs=['service.peach_arrival'])
    audit=FactVerification(True,scope_check={'unwanted_parts':[tail]},claim_checks=[
        {'claim':'第一天林芝接機','supported':True,'evidence':'service.peach_arrival'}])
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('exact pruning'))
    result,_,_=revise_copy({},decision,audit,set(decision.evidence_refs))
    assert result['reply']=='第一天林芝接機。'
    assert result['follow_up_question']=='' and result['follow_up_type']=='none'


@pytest.mark.parametrize('question,body,blocked',[
    ('台灣籍，媽媽75爸爸76，兩位都能參加嗎？','75歲可以參加，76歲不建議。',True),
    ('台灣籍，媽媽75爸爸76，兩位都能參加嗎？','75歲可以參加，需健康證明；76歲不建議。',False),
    ('台灣人64歲能去嗎？','64歲不會因為未滿65歲而不能報名。',False),
    ('香港人75歲能去嗎？','證件條件需要核對。',False),
])
def test_age_qualification_survives_incorrect_semantic_pass(monkeypatch,question,body,blocked):
    from app.reception_v2 import runtime
    from app.reply_fact_verification import FactVerification
    from app.deepseek_evaluation import EvaluationDecision
    travelers=([{'age':75,'taiwan_traveler':True},{'age':76,'taiwan_traveler':True}] if '媽媽' in question
               else [{'age':64,'taiwan_traveler':True}] if '64' in question
               else [{'age':75,'taiwan_traveler':False}])
    monkeypatch.setattr(runtime,'call_reply_fact_verifier',lambda *a:(
        FactVerification(True,scope_check={'traveler_age_checks':travelers}),[],''))
    decision=EvaluationDecision('reply','peach_9d','other',reply=body,reply_body=body,
        route_variant='peach_9d_2027',evidence_refs=['service.peach_age'])
    result,_,_=runtime._verify({'module':'reply','customer_text':question},decision)
    assert any('健康证明条件' in v for v in result.contract_violations)==blocked
